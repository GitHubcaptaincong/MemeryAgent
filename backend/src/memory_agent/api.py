from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_agent.bootstrap import ensure_local_identity
from memory_agent.config import Settings, get_settings
from memory_agent.database import SessionLocal, get_session
from memory_agent.models import (
    AgentEvent,
    AgentRun,
    KnowledgeDraft,
    MemoryCandidate,
    RunState,
    TERMINAL_RUN_STATES,
)
from memory_agent.runtime import get_job_runner
from memory_agent.review import (
    get_reminder_preference,
    get_review_overview,
    list_due_review_cards,
    list_review_history,
    rate_review_answer,
    submit_review_answer,
    update_reminder_preference,
)
from memory_agent.schemas import (
    DraftRead,
    EventRead,
    HealthRead,
    MemoryCandidateRead,
    MemoryDecision,
    ReminderPreferenceRead,
    ReminderPreferenceUpdate,
    ReviewAnswerCreate,
    ReviewAnswerRead,
    ReviewCardRead,
    ReviewHistoryRead,
    ReviewOverviewRead,
    ReviewRatingCreate,
    ReviewResultRead,
    RunCreate,
    RunRead,
    SourceCreate,
    SourceRead,
)
from memory_agent.services import (
    confirm_draft,
    create_run,
    create_source,
    decide_memory_candidate,
    get_draft_for_user,
)


router = APIRouter(prefix="/api/v1")


def _identity(session: Session, settings: Settings):
    return ensure_local_identity(session, settings)


def _run_for_user(session: Session, run_id: UUID, user_id: UUID) -> AgentRun:
    run = session.scalar(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user_id)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/health", response_model=HealthRead)
def health(settings: Settings = Depends(get_settings)) -> HealthRead:
    return HealthRead(
        status="ok",
        model_provider=settings.model_provider,
        model_name=settings.model_name,
        database="sqlite" if settings.is_sqlite else "postgresql",
    )


@router.get("/review/queue", response_model=list[ReviewCardRead])
def get_review_queue(
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[ReviewCardRead]:
    user, _ = _identity(session, settings)
    return [
        ReviewCardRead.model_validate(item)
        for item in list_due_review_cards(session, user_id=user.id, limit=limit)
    ]


@router.get("/review/overview", response_model=ReviewOverviewRead)
def get_review_overview_route(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ReviewOverviewRead:
    user, _ = _identity(session, settings)
    return ReviewOverviewRead.model_validate(
        get_review_overview(session, user_id=user.id)
    )


@router.get("/review/history", response_model=list[ReviewHistoryRead])
def get_review_history(
    limit: int = Query(default=10, ge=1, le=100),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[ReviewHistoryRead]:
    user, _ = _identity(session, settings)
    return [
        ReviewHistoryRead.model_validate(item)
        for item in list_review_history(session, user_id=user.id, limit=limit)
    ]


@router.post(
    "/review/cards/{card_id}/answers",
    response_model=ReviewAnswerRead,
    status_code=status.HTTP_201_CREATED,
)
def post_review_answer(
    card_id: UUID,
    data: ReviewAnswerCreate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ReviewAnswerRead:
    user, _ = _identity(session, settings)
    try:
        result = submit_review_answer(
            session,
            user_id=user.id,
            card_id=card_id,
            answer=data.answer,
            idempotency_key=data.idempotency_key,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ReviewAnswerRead.model_validate(result)


@router.post("/review/cards/{card_id}/ratings", response_model=ReviewResultRead)
def post_review_rating(
    card_id: UUID,
    data: ReviewRatingCreate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ReviewResultRead:
    user, _ = _identity(session, settings)
    try:
        result = rate_review_answer(
            session,
            user_id=user.id,
            card_id=card_id,
            attempt_id=data.attempt_id,
            rating=data.rating,
            idempotency_key=data.idempotency_key,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ReviewResultRead.model_validate(result)


@router.get("/reminders/preferences", response_model=ReminderPreferenceRead)
def get_reminder_settings(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ReminderPreferenceRead:
    user, _ = _identity(session, settings)
    return ReminderPreferenceRead.model_validate(
        get_reminder_preference(session, user_id=user.id)
    )


@router.put("/reminders/preferences", response_model=ReminderPreferenceRead)
def put_reminder_settings(
    data: ReminderPreferenceUpdate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ReminderPreferenceRead:
    user, _ = _identity(session, settings)
    return ReminderPreferenceRead.model_validate(
        update_reminder_preference(
            session,
            user_id=user.id,
            enabled=data.enabled,
            preferred_time=data.preferred_time,
            daily_limit=data.daily_limit,
            overdue_enabled=data.overdue_enabled,
            timezone=data.timezone,
        )
    )


@router.post("/sources", response_model=SourceRead, status_code=status.HTTP_201_CREATED)
def post_source(
    data: SourceCreate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SourceRead:
    user, _profile = _identity(session, settings)
    source = create_source(session, user_id=user.id, data=data)
    return SourceRead.model_validate(source)


@router.post("/runs", response_model=RunRead, status_code=status.HTTP_202_ACCEPTED)
def post_run(
    data: RunCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RunRead:
    user, profile = _identity(session, settings)
    try:
        run, job, created = create_run(
            session,
            user_id=user.id,
            profile=profile,
            source_id=data.source_id,
            idempotency_key=data.idempotency_key,
            settings=settings,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if created and settings.inline_worker:
        background_tasks.add_task(get_job_runner().run_job, job.id)
    return RunRead.model_validate(run)


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(
    run_id: UUID,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RunRead:
    user, _ = _identity(session, settings)
    return RunRead.model_validate(_run_for_user(session, run_id, user.id))


@router.post("/runs/{run_id}/cancel", response_model=RunRead)
def cancel_run(
    run_id: UUID,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RunRead:
    user, _ = _identity(session, settings)
    run = _run_for_user(session, run_id, user.id)
    if run.state not in TERMINAL_RUN_STATES and run.state != RunState.AWAITING_USER.value:
        run.cancel_requested = True
        session.commit()
    return RunRead.model_validate(run)


@router.get("/runs/{run_id}/events", response_model=list[EventRead])
def list_events(
    run_id: UUID,
    after_seq: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[EventRead]:
    user, _ = _identity(session, settings)
    _run_for_user(session, run_id, user.id)
    events = session.scalars(
        select(AgentEvent)
        .where(
            AgentEvent.run_id == run_id,
            AgentEvent.visible_to_user.is_(True),
            AgentEvent.seq > after_seq,
        )
        .order_by(AgentEvent.seq)
    ).all()
    return [EventRead.model_validate(event) for event in events]


async def _event_stream(run_id: UUID, user_id: UUID, after_seq: int) -> AsyncIterator[str]:
    cursor = after_seq
    idle_ticks = 0
    while True:
        session = SessionLocal()
        try:
            run = session.scalar(
                select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user_id)
            )
            if run is None:
                yield "event: error\ndata: {\"detail\":\"run not found\"}\n\n"
                return
            events = session.scalars(
                select(AgentEvent)
                .where(
                    AgentEvent.run_id == run_id,
                    AgentEvent.visible_to_user.is_(True),
                    AgentEvent.seq > cursor,
                )
                .order_by(AgentEvent.seq)
            ).all()
            for event in events:
                cursor = event.seq
                payload = EventRead.model_validate(event).model_dump(mode="json")
                yield (
                    f"id: {event.seq}\n"
                    f"event: {event.event_type}\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
            if events:
                idle_ticks = 0
            else:
                idle_ticks += 1
            done = run.state in TERMINAL_RUN_STATES or run.state == RunState.AWAITING_USER.value
            if done and not events:
                yield f"event: stream.closed\ndata: {{\"state\":\"{run.state}\"}}\n\n"
                return
            if idle_ticks >= 4:
                started_at = run.started_at
                if started_at is not None and started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=UTC)
                elapsed_seconds = int(
                    (datetime.now(UTC) - started_at).total_seconds()
                    if started_at is not None
                    else 0
                )
                pulse = {
                    "state": run.state,
                    "elapsed_seconds": elapsed_seconds,
                    "message": _progress_message(run.state, elapsed_seconds),
                }
                yield (
                    "event: progress.pulse\n"
                    f"data: {json.dumps(pulse, ensure_ascii=False)}\n\n"
                )
                idle_ticks = 0
        finally:
            session.close()
        await asyncio.sleep(0.5)


def _progress_message(state: str, elapsed_seconds: int) -> str:
    if state == RunState.QUEUED.value:
        return "任务已收到，正在等待 Worker 接管"
    if state in {RunState.INGESTING.value, RunState.RETRIEVING_MEMORY.value}:
        return "正在读取材料和已批准记忆"
    if state in {RunState.ROUTING_SKILLS.value, RunState.PLANNING.value}:
        return "正在选择处理方式并制定可审计计划"
    if state in {
        RunState.EXECUTING.value,
        RunState.DRAFTING.value,
        RunState.REVIEWING.value,
    }:
        return f"模型正在生成并校验草稿，已处理 {elapsed_seconds} 秒"
    if state == RunState.RETRY_WAIT.value:
        return "模型服务暂时不可用，正在等待自动重试"
    return f"Agent 正在处理，已运行 {elapsed_seconds} 秒"


@router.get("/runs/{run_id}/events/stream")
def stream_events(
    run_id: UUID,
    after_seq: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    user, _ = _identity(session, settings)
    _run_for_user(session, run_id, user.id)
    return StreamingResponse(
        _event_stream(run_id, user.id, after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/draft", response_model=DraftRead)
def get_run_draft(
    run_id: UUID,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DraftRead:
    user, _ = _identity(session, settings)
    _run_for_user(session, run_id, user.id)
    draft_id = session.scalar(
        select(KnowledgeDraft.id)
        .where(KnowledgeDraft.run_id == run_id, KnowledgeDraft.user_id == user.id)
        .order_by(KnowledgeDraft.version.desc())
        .limit(1)
    )
    if draft_id is None:
        raise HTTPException(status_code=404, detail="draft not ready")
    return DraftRead.model_validate(
        get_draft_for_user(session, draft_id=draft_id, user_id=user.id)
    )


@router.post("/drafts/{draft_id}/confirm", response_model=DraftRead)
def post_confirm_draft(
    draft_id: UUID,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DraftRead:
    user, _ = _identity(session, settings)
    try:
        draft, _candidate = confirm_draft(session, draft_id=draft_id, user_id=user.id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return DraftRead.model_validate(
        get_draft_for_user(session, draft_id=draft.id, user_id=user.id)
    )


@router.get("/memory-candidates", response_model=list[MemoryCandidateRead])
def list_memory_candidates(
    candidate_status: str = Query(default="pending", alias="status"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[MemoryCandidateRead]:
    user, _ = _identity(session, settings)
    candidates = session.scalars(
        select(MemoryCandidate)
        .where(
            MemoryCandidate.user_id == user.id,
            MemoryCandidate.status == candidate_status,
        )
        .order_by(MemoryCandidate.created_at.desc())
    ).all()
    return [MemoryCandidateRead.model_validate(item) for item in candidates]


@router.post("/memory-candidates/{candidate_id}/decision", response_model=MemoryCandidateRead)
def post_memory_decision(
    candidate_id: UUID,
    data: MemoryDecision,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> MemoryCandidateRead:
    user, _ = _identity(session, settings)
    try:
        candidate = decide_memory_candidate(
            session,
            candidate_id=candidate_id,
            user_id=user.id,
            decision=data.decision,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return MemoryCandidateRead.model_validate(candidate)
