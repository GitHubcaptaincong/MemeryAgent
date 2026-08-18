from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from memory_agent.auth import IdentityContext, get_current_identity
from memory_agent.analytics import get_daily_plan, get_review_insights
from memory_agent.config import Settings, get_settings
from memory_agent.database import SessionLocal, get_session
from memory_agent.models import (
    AgentEvent,
    AgentRun,
    Conversation,
    ConversationTurn,
    DraftUnit,
    KnowledgeDraft,
    MemoryCandidate,
    RunState,
    TERMINAL_RUN_STATES,
)
from memory_agent.runtime import get_job_runner
from memory_agent.review import (
    get_reminder_preference,
    get_answer_evaluation,
    get_answer_evaluation_job,
    get_review_overview,
    list_due_review_cards,
    list_review_history,
    rate_review_answer,
    submit_review_answer,
    update_reminder_preference,
)
from memory_agent.schemas import (
    DraftRead,
    ConversationDetailRead,
    ConversationRead,
    ConversationTurnCreate,
    ConversationTurnHistoryRead,
    ConversationTurnRead,
    ConversationTurnStartRead,
    ConversationUpdate,
    EventRead,
    HealthRead,
    MemoryCandidateRead,
    MemoryDecision,
    ReminderPreferenceRead,
    ReminderPreferenceUpdate,
    ReminderSubscriptionGrantCreate,
    ReminderSubscriptionStatusRead,
    ReminderDispatchClaim,
    ReminderDispatchResult,
    ReviewAnswerCreate,
    ReviewAnswerRead,
    ReviewEvaluationRead,
    ReviewCardRead,
    ReviewHistoryRead,
    ReviewOverviewRead,
    ReviewRatingCreate,
    ReviewResultRead,
    RunCreate,
    RunRead,
    SourceCreate,
    SourceRead,
    SourceResolveCreate,
    SourceUrlCreate,
)
from memory_agent.services import (
    confirm_draft,
    create_run,
    create_source,
    decide_memory_candidate,
    get_draft_for_user,
)
from memory_agent.source_ingestion import (
    SourceFetchError,
    detect_standalone_url,
    fetch_public_source,
)
from memory_agent.reminders import (
    claim_due_reminders,
    record_delivery_result,
    record_subscription_result,
    reminder_status,
    verify_dispatch_token,
)


router = APIRouter(prefix="/api/v1")


def _identity(identity: IdentityContext):
    return identity.user, identity.profile


def _run_for_user(session: Session, run_id: UUID, user_id: UUID) -> AgentRun:
    run = session.scalar(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user_id)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


def _conversation_for_user(
    session: Session,
    *,
    conversation_id: UUID,
    user_id: UUID,
) -> Conversation:
    conversation = session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conversation


def _draft_for_run(
    session: Session,
    *,
    run_id: UUID | None,
    user_id: UUID,
) -> KnowledgeDraft | None:
    if run_id is None:
        return None
    return session.scalar(
        select(KnowledgeDraft)
        .where(KnowledgeDraft.run_id == run_id, KnowledgeDraft.user_id == user_id)
        .order_by(KnowledgeDraft.version.desc())
        .options(selectinload(KnowledgeDraft.units).selectinload(DraftUnit.evidence))
        .limit(1)
    )


def _refresh_conversation_title(
    session: Session,
    *,
    conversation: Conversation,
    user_id: UUID,
) -> None:
    if conversation.title_status != "pending" or conversation.turn_count == 0:
        return
    first_turn = session.scalar(
        select(ConversationTurn)
        .where(ConversationTurn.conversation_id == conversation.id)
        .order_by(ConversationTurn.position.asc())
        .limit(1)
    )
    if first_turn is None:
        return
    draft = _draft_for_run(session, run_id=first_turn.run_id, user_id=user_id)
    if draft is None or not draft.units:
        return
    generated_title = draft.units[0].title.strip()
    if not generated_title:
        return
    conversation.title = generated_title[:100]
    conversation.title_status = "generated"
    conversation.updated_at = datetime.now(UTC)
    session.commit()


def _conversation_turn_read(
    session: Session,
    *,
    turn: ConversationTurn,
    user_id: UUID,
) -> ConversationTurnRead:
    run = session.get(AgentRun, turn.run_id) if turn.run_id else None
    draft = _draft_for_run(session, run_id=turn.run_id, user_id=user_id)
    assistant_summary = None
    if draft is not None:
        assistant_summary = str(draft.agent_summary.get("overview") or "知识草稿已生成。")
    elif run is not None and run.state == RunState.FAILED.value:
        assistant_summary = run.error_message or "这轮整理失败了，可以重新发送材料。"
    return ConversationTurnRead(
        id=turn.id,
        position=turn.position,
        user_content=turn.user_content,
        source_id=turn.source_id,
        run_id=turn.run_id,
        run_state=run.state if run else None,
        assistant_summary=assistant_summary,
        draft=DraftRead.model_validate(draft) if draft else None,
        created_at=turn.created_at,
    )


@router.get("/health", response_model=HealthRead)
def health(settings: Settings = Depends(get_settings)) -> HealthRead:
    return HealthRead(
        status="ok",
        model_provider=settings.model_provider,
        model_name=settings.model_name,
        database="sqlite" if settings.is_sqlite else "postgresql",
    )


@router.get("/ready")
def ready(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ready"}


@router.get("/review/queue", response_model=list[ReviewCardRead])
def get_review_queue(
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> list[ReviewCardRead]:
    user, _ = _identity(identity)
    return [
        ReviewCardRead.model_validate(item)
        for item in list_due_review_cards(session, user_id=user.id, limit=limit)
    ]


@router.get("/review/overview", response_model=ReviewOverviewRead)
def get_review_overview_route(
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> ReviewOverviewRead:
    user, _ = _identity(identity)
    return ReviewOverviewRead.model_validate(
        get_review_overview(session, user_id=user.id)
    )


@router.get("/review/history", response_model=list[ReviewHistoryRead])
def get_review_history(
    limit: int = Query(default=10, ge=1, le=100),
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> list[ReviewHistoryRead]:
    user, _ = _identity(identity)
    return [
        ReviewHistoryRead.model_validate(item)
        for item in list_review_history(session, user_id=user.id, limit=limit)
    ]


@router.get("/review/insights")
def get_review_insights_route(
    trend_days: int = Query(default=30, ge=1, le=365),
    forecast_days: int = Query(default=14, ge=1, le=90),
    weak_limit: int = Query(default=10, ge=0, le=100),
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> dict[str, object]:
    preference = get_reminder_preference(session, user_id=identity.user.id)
    return get_review_insights(
        session,
        user_id=identity.user.id,
        trend_days=trend_days,
        forecast_days=forecast_days,
        weak_limit=weak_limit,
        timezone=preference.timezone,
        daily_limit=preference.daily_limit,
    )


@router.get("/review/daily-plan")
def get_review_daily_plan(
    include_overflow: bool = Query(default=False),
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> dict[str, object]:
    preference = get_reminder_preference(session, user_id=identity.user.id)
    return get_daily_plan(
        session,
        user_id=identity.user.id,
        daily_limit=preference.daily_limit,
        timezone=preference.timezone,
        overdue_enabled=preference.overdue_enabled,
        include_overflow=include_overflow,
    )


@router.post(
    "/review/cards/{card_id}/answers",
    response_model=ReviewAnswerRead,
    status_code=status.HTTP_201_CREATED,
)
def post_review_answer(
    card_id: UUID,
    data: ReviewAnswerCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> ReviewAnswerRead:
    user, _ = _identity(identity)
    try:
        result, job = submit_review_answer(
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
    if job is not None and identity.settings.inline_worker:
        # The durable job remains recoverable even if this request is interrupted.
        background_tasks.add_task(get_job_runner().run_job, job.id)
    return ReviewAnswerRead.model_validate(result)


@router.get(
    "/review/cards/{card_id}/attempts/{attempt_id}/evaluation",
    response_model=ReviewEvaluationRead,
)
def get_review_evaluation(
    card_id: UUID,
    attempt_id: UUID,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> ReviewEvaluationRead:
    try:
        result = get_answer_evaluation(
            session,
            user_id=identity.user.id,
            card_id=card_id,
            attempt_id=attempt_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result["status"] == "pending" and identity.settings.inline_worker:
        job = get_answer_evaluation_job(
            session,
            user_id=identity.user.id,
            attempt_id=attempt_id,
        )
        if job is not None:
            background_tasks.add_task(get_job_runner().run_job, job.id)
    return ReviewEvaluationRead.model_validate(result)


@router.post("/review/cards/{card_id}/ratings", response_model=ReviewResultRead)
def post_review_rating(
    card_id: UUID,
    data: ReviewRatingCreate,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> ReviewResultRead:
    user, _ = _identity(identity)
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
    identity: IdentityContext = Depends(get_current_identity),
) -> ReminderPreferenceRead:
    user, _ = _identity(identity)
    return ReminderPreferenceRead.model_validate(
        get_reminder_preference(session, user_id=user.id)
    )


@router.put("/reminders/preferences", response_model=ReminderPreferenceRead)
def put_reminder_settings(
    data: ReminderPreferenceUpdate,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> ReminderPreferenceRead:
    user, _ = _identity(identity)
    return ReminderPreferenceRead.model_validate(
        update_reminder_preference(
            session,
            user_id=user.id,
            enabled=data.enabled,
            preferred_time=data.preferred_time,
            daily_limit=data.daily_limit,
            overdue_enabled=data.overdue_enabled,
            ai_evaluation_enabled=data.ai_evaluation_enabled,
            timezone=data.timezone,
        )
    )


@router.post("/reminders/subscription-grants", response_model=ReminderSubscriptionStatusRead)
def post_reminder_subscription_grant(
    data: ReminderSubscriptionGrantCreate,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> ReminderSubscriptionStatusRead:
    expected_template = identity.settings.wechat_subscribe_template_id
    if not expected_template or data.template_id != expected_template:
        raise HTTPException(status_code=409, detail="subscription template is not configured")
    try:
        record_subscription_result(
            session,
            user_id=identity.user.id,
            template_id=data.template_id,
            result=data.result,
            idempotency_key=data.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ReminderSubscriptionStatusRead.model_validate(
        reminder_status(session, user_id=identity.user.id, settings=identity.settings)
    )


@router.get("/reminders/status", response_model=ReminderSubscriptionStatusRead)
def get_reminder_subscription_status(
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> ReminderSubscriptionStatusRead:
    return ReminderSubscriptionStatusRead.model_validate(
        reminder_status(session, user_id=identity.user.id, settings=identity.settings)
    )


@router.post("/internal/reminders/dispatch/claim")
def post_reminder_dispatch_claim(
    data: ReminderDispatchClaim,
    x_reminder_dispatch_token: str | None = Header(default=None),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, list[dict[str, object]]]:
    verify_dispatch_token(x_reminder_dispatch_token, settings)
    return {
        "jobs": claim_due_reminders(
            session,
            settings=settings,
            batch_size=min(data.batch_size, settings.reminder_batch_size),
            lease_seconds=min(data.lease_seconds, settings.reminder_lease_seconds),
        )
    }


@router.post("/internal/reminders/dispatch/{delivery_id}/result")
def post_reminder_dispatch_result(
    delivery_id: UUID,
    data: ReminderDispatchResult,
    x_reminder_dispatch_token: str | None = Header(default=None),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    verify_dispatch_token(x_reminder_dispatch_token, settings)
    try:
        delivery = record_delivery_result(
            session,
            delivery_id=delivery_id,
            result_status=data.status,
            wechat_errcode=data.wechat_errcode,
            wechat_errmsg=data.wechat_errmsg,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": str(delivery.id), "status": delivery.status}


@router.post("/sources", response_model=SourceRead, status_code=status.HTTP_201_CREATED)
def post_source(
    data: SourceCreate,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> SourceRead:
    user, _profile = _identity(identity)
    source = create_source(session, user_id=user.id, data=data)
    return SourceRead.model_validate(source)


def _create_source_from_url(
    *,
    data: SourceUrlCreate,
    session: Session,
    user_id: UUID,
    settings: Settings,
):
    try:
        fetched = fetch_public_source(
            data.url,
            max_chars=settings.source_max_chars,
            max_bytes=settings.source_fetch_max_bytes,
            timeout_seconds=settings.source_fetch_timeout_seconds,
            max_redirects=settings.source_fetch_max_redirects,
        )
    except SourceFetchError as exc:
        status_code = status.HTTP_502_BAD_GATEWAY if exc.retryable else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    title = (data.title or fetched.title or urlsplit(fetched.final_url).hostname or "公开链接").strip()
    return create_source(
        session,
        user_id=user_id,
        data=SourceCreate(
            title=title[:300],
            learning_goal=data.learning_goal,
            content=fetched.content,
            content_type="text",
            web_access_allowed=data.web_access_allowed,
        ),
        origin_type="url",
        origin_url=fetched.final_url,
        retrieved_at=fetched.retrieved_at,
        origin_content_hash=fetched.response_hash,
    )


@router.post("/sources/from-url", response_model=SourceRead, status_code=status.HTTP_201_CREATED)
def post_source_from_url(
    data: SourceUrlCreate,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> SourceRead:
    user, _profile = _identity(identity)
    source = _create_source_from_url(
        data=data,
        session=session,
        user_id=user.id,
        settings=identity.settings,
    )
    return SourceRead.model_validate(source)


def _resolve_source_data(
    *,
    data: SourceResolveCreate,
    session: Session,
    user_id: UUID,
    settings: Settings,
):
    url = detect_standalone_url(data.input)
    if url is not None:
        if len(url) > 2_048:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "invalid_url", "message": "公开链接不能超过 2,048 个字符。"},
            )
        return _create_source_from_url(
            data=SourceUrlCreate(
                url=url,
                title=data.title,
                learning_goal=data.learning_goal,
                web_access_allowed=data.web_access_allowed,
            ),
            session=session,
            user_id=user_id,
            settings=settings,
        )
    return create_source(
        session,
        user_id=user_id,
        data=SourceCreate(
            title=data.title or "快速记录",
            learning_goal=data.learning_goal,
            content=data.input,
            content_type=data.content_type,
            web_access_allowed=data.web_access_allowed,
        ),
    )


@router.post("/sources/resolve", response_model=SourceRead, status_code=status.HTTP_201_CREATED)
def post_source_resolve(
    data: SourceResolveCreate,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> SourceRead:
    user, _profile = _identity(identity)
    source = _resolve_source_data(
        data=data,
        session=session,
        user_id=user.id,
        settings=identity.settings,
    )
    return SourceRead.model_validate(source)


@router.post("/conversations", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
def post_conversation(
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> ConversationRead:
    conversation = Conversation(user_id=identity.user.id)
    session.add(conversation)
    session.commit()
    return ConversationRead.model_validate(conversation)


@router.get("/conversations", response_model=list[ConversationRead])
def get_conversations(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    include_empty: bool = Query(default=False),
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> list[ConversationRead]:
    query = select(Conversation).where(Conversation.user_id == identity.user.id)
    if not include_empty:
        query = query.where(Conversation.turn_count > 0)
    conversations = list(
        session.scalars(
            query.order_by(Conversation.updated_at.desc()).offset(offset).limit(limit)
        )
    )
    for conversation in conversations:
        _refresh_conversation_title(
            session,
            conversation=conversation,
            user_id=identity.user.id,
        )
    conversations.sort(key=lambda item: item.updated_at, reverse=True)
    return [ConversationRead.model_validate(item) for item in conversations]


@router.patch("/conversations/{conversation_id}", response_model=ConversationRead)
def patch_conversation(
    conversation_id: UUID,
    data: ConversationUpdate,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> ConversationRead:
    conversation = _conversation_for_user(
        session,
        conversation_id=conversation_id,
        user_id=identity.user.id,
    )
    title = data.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="conversation title cannot be blank")
    conversation.title = title
    conversation.title_status = "custom"
    conversation.updated_at = datetime.now(UTC)
    session.commit()
    return ConversationRead.model_validate(conversation)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: UUID,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> Response:
    conversation = _conversation_for_user(
        session,
        conversation_id=conversation_id,
        user_id=identity.user.id,
    )
    session.delete(conversation)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailRead)
def get_conversation(
    conversation_id: UUID,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> ConversationDetailRead:
    conversation = _conversation_for_user(
        session,
        conversation_id=conversation_id,
        user_id=identity.user.id,
    )
    _refresh_conversation_title(
        session,
        conversation=conversation,
        user_id=identity.user.id,
    )
    turns = list(
        session.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.conversation_id == conversation.id)
            .order_by(ConversationTurn.position.asc())
        )
    )
    return ConversationDetailRead(
        conversation=ConversationRead.model_validate(conversation),
        turns=[
            _conversation_turn_read(session, turn=turn, user_id=identity.user.id)
            for turn in turns
        ],
    )


@router.get(
    "/conversations/{conversation_id}/turns",
    response_model=ConversationTurnHistoryRead,
)
def get_conversation_turns(
    conversation_id: UUID,
    after_position: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> ConversationTurnHistoryRead:
    conversation = _conversation_for_user(
        session,
        conversation_id=conversation_id,
        user_id=identity.user.id,
    )
    turns = list(
        session.scalars(
            select(ConversationTurn)
            .where(
                ConversationTurn.conversation_id == conversation.id,
                ConversationTurn.position > after_position,
            )
            .order_by(ConversationTurn.position.asc())
            .limit(limit + 1)
        )
    )
    has_more = len(turns) > limit
    page = turns[:limit]
    return ConversationTurnHistoryRead(
        items=[
            _conversation_turn_read(session, turn=turn, user_id=identity.user.id)
            for turn in page
        ],
        next_after_position=page[-1].position if has_more and page else None,
    )


@router.post(
    "/conversations/{conversation_id}/turns",
    response_model=ConversationTurnStartRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def post_conversation_turn(
    conversation_id: UUID,
    data: ConversationTurnCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> ConversationTurnStartRead:
    user, profile = _identity(identity)
    conversation = _conversation_for_user(
        session,
        conversation_id=conversation_id,
        user_id=user.id,
    )
    existing = session.scalar(
        select(ConversationTurn).where(
            ConversationTurn.conversation_id == conversation.id,
            ConversationTurn.idempotency_key == data.idempotency_key,
        )
    )
    if existing is not None:
        if existing.run_id is None:
            raise HTTPException(status_code=409, detail="conversation turn has no run")
        run = _run_for_user(session, existing.run_id, user.id)
        return ConversationTurnStartRead(
            conversation=ConversationRead.model_validate(conversation),
            turn=_conversation_turn_read(session, turn=existing, user_id=user.id),
            run=RunRead.model_validate(run),
        )

    source = _resolve_source_data(
        data=SourceResolveCreate(
            input=data.input,
            title=data.title,
            learning_goal=data.learning_goal,
            content_type=data.content_type,
            web_access_allowed=data.web_access_allowed,
        ),
        session=session,
        user_id=user.id,
        settings=identity.settings,
    )
    run_key = hashlib.sha256(
        f"{conversation.id}:{data.idempotency_key}".encode("utf-8")
    ).hexdigest()
    run, job, created = create_run(
        session,
        user_id=user.id,
        profile=profile,
        source_id=source.id,
        idempotency_key=run_key,
        settings=identity.settings,
    )
    position = int(
        session.scalar(
            select(func.coalesce(func.max(ConversationTurn.position), 0)).where(
                ConversationTurn.conversation_id == conversation.id
            )
        )
        or 0
    ) + 1
    turn = ConversationTurn(
        conversation_id=conversation.id,
        position=position,
        idempotency_key=data.idempotency_key,
        user_content=data.input.strip(),
        source_id=source.id,
        run_id=run.id,
    )
    session.add(turn)
    conversation.turn_count = position
    conversation.updated_at = datetime.now(UTC)
    session.commit()
    if created and identity.settings.inline_worker:
        background_tasks.add_task(get_job_runner().run_job, job.id)
    return ConversationTurnStartRead(
        conversation=ConversationRead.model_validate(conversation),
        turn=_conversation_turn_read(session, turn=turn, user_id=user.id),
        run=RunRead.model_validate(run),
    )


@router.post("/runs", response_model=RunRead, status_code=status.HTTP_202_ACCEPTED)
def post_run(
    data: RunCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> RunRead:
    user, profile = _identity(identity)
    settings = identity.settings
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
    identity: IdentityContext = Depends(get_current_identity),
) -> RunRead:
    user, _ = _identity(identity)
    return RunRead.model_validate(_run_for_user(session, run_id, user.id))


@router.post("/runs/{run_id}/cancel", response_model=RunRead)
def cancel_run(
    run_id: UUID,
    session: Session = Depends(get_session),
    identity: IdentityContext = Depends(get_current_identity),
) -> RunRead:
    user, _ = _identity(identity)
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
    identity: IdentityContext = Depends(get_current_identity),
) -> list[EventRead]:
    user, _ = _identity(identity)
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
    identity: IdentityContext = Depends(get_current_identity),
) -> StreamingResponse:
    user, _ = _identity(identity)
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
    identity: IdentityContext = Depends(get_current_identity),
) -> DraftRead:
    user, _ = _identity(identity)
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
    identity: IdentityContext = Depends(get_current_identity),
) -> DraftRead:
    user, _ = _identity(identity)
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
    identity: IdentityContext = Depends(get_current_identity),
) -> list[MemoryCandidateRead]:
    user, _ = _identity(identity)
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
    identity: IdentityContext = Depends(get_current_identity),
) -> MemoryCandidateRead:
    user, _ = _identity(identity)
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
