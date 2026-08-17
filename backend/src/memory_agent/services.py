from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from memory_agent.config import Settings
from memory_agent.events import append_event, transition_run
from memory_agent.models import (
    AgentProfile,
    AgentRun,
    BackgroundJob,
    DraftUnit,
    KnowledgeDraft,
    MemoryCandidate,
    MemoryEvidence,
    MemoryItem,
    RetrievalDocument,
    RunState,
    Source,
    SourceChunk,
)
from memory_agent.schemas import SourceCreate
from memory_agent.review import create_review_cards_for_draft
from memory_agent.review_config import FSRS_SCHEDULER_VERSION


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _chunk_source(content: str, *, size: int = 2_000, overlap: int = 200):
    start = 0
    index = 0
    while start < len(content):
        end = min(len(content), start + size)
        yield index, start, end, content[start:end]
        if end == len(content):
            break
        start = end - overlap
        index += 1


def create_source(
    session: Session,
    *,
    user_id: UUID,
    data: SourceCreate,
    origin_type: str = "text",
    origin_url: str | None = None,
    retrieved_at: datetime | None = None,
    origin_content_hash: str | None = None,
) -> Source:
    normalized = data.content.replace("\r\n", "\n").strip()
    source = Source(
        user_id=user_id,
        title=data.title.strip(),
        learning_goal=data.learning_goal.strip(),
        raw_content=normalized,
        content_type=data.content_type,
        origin_type=origin_type,
        origin_url=origin_url,
        retrieved_at=retrieved_at,
        origin_content_hash=origin_content_hash,
        content_hash=_sha256(normalized),
        char_count=len(normalized),
        web_access_allowed=data.web_access_allowed,
    )
    session.add(source)
    session.flush()
    for index, start, end, content in _chunk_source(normalized):
        session.add(
            SourceChunk(
                source_id=source.id,
                chunk_index=index,
                start_char=start,
                end_char=end,
                content=content,
                content_hash=_sha256(content),
            )
        )
    session.commit()
    return source


def create_run(
    session: Session,
    *,
    user_id: UUID,
    profile: AgentProfile,
    source_id: UUID,
    idempotency_key: str | None,
    settings: Settings,
) -> tuple[AgentRun, BackgroundJob, bool]:
    source = session.scalar(
        select(Source).where(Source.id == source_id, Source.user_id == user_id)
    )
    if source is None:
        raise LookupError("source not found")
    key = idempotency_key or f"run-{source_id}-{uuid.uuid4()}"
    existing = session.scalar(
        select(AgentRun).where(
            AgentRun.user_id == user_id,
            AgentRun.idempotency_key == key,
        )
    )
    if existing is not None:
        job = session.scalar(
            select(BackgroundJob).where(
                BackgroundJob.run_id == existing.id,
                BackgroundJob.job_type == "agent_run",
            )
        )
        if job is None:
            raise RuntimeError("idempotent run exists without its persistent job")
        return existing, job, False

    run = AgentRun(
        user_id=user_id,
        profile_id=profile.id,
        source_id=source.id,
        state=RunState.CREATED.value,
        idempotency_key=key,
        model_provider=settings.model_provider,
        model_name=settings.model_name,
        config_json={
            "max_tool_calls": settings.agent_max_tool_calls,
            "max_web_searches": settings.agent_max_web_searches,
            "max_revisions": settings.agent_max_revisions,
            "web_access_allowed": source.web_access_allowed,
            "memory_write_requires_approval": True,
        },
    )
    session.add(run)
    session.flush()
    append_event(
        session,
        run,
        "run.created",
        {
            "source_id": str(source.id),
            "model_provider": settings.model_provider,
            "budgets": run.config_json,
        },
    )
    run.state = RunState.QUEUED.value
    append_event(
        session,
        run,
        "run.state_changed",
        {"state": RunState.QUEUED.value, "message": "任务已进入队列"},
    )
    job = BackgroundJob(
        run_id=run.id,
        job_type="agent_run",
        payload_json={"run_id": str(run.id)},
    )
    session.add(job)
    session.commit()
    return run, job, True


def get_draft_for_user(session: Session, *, draft_id: UUID, user_id: UUID) -> KnowledgeDraft:
    draft = session.scalar(
        select(KnowledgeDraft)
        .where(KnowledgeDraft.id == draft_id, KnowledgeDraft.user_id == user_id)
        .options(selectinload(KnowledgeDraft.units).selectinload(DraftUnit.evidence))
    )
    if draft is None:
        raise LookupError("draft not found")
    return draft


def confirm_draft(
    session: Session,
    *,
    draft_id: UUID,
    user_id: UUID,
) -> tuple[KnowledgeDraft, MemoryCandidate]:
    draft = get_draft_for_user(session, draft_id=draft_id, user_id=user_id)
    if draft.status == "confirmed":
        create_review_cards_for_draft(session, draft=draft, user_id=user_id)
        session.commit()
        candidate = session.scalar(
            select(MemoryCandidate).where(
                MemoryCandidate.run_id == draft.run_id,
                MemoryCandidate.canonical_key == "active_learning_goal",
            )
        )
        if candidate is None:
            raise RuntimeError("confirmed draft is missing its memory candidate")
        return draft, candidate
    draft.status = "confirmed"
    draft.confirmed_at = datetime.now(UTC)
    run = session.get(AgentRun, draft.run_id)
    if run is None:
        raise RuntimeError("draft run not found")
    transition_run(session, run, RunState.CONFIRMED)
    review_cards = create_review_cards_for_draft(
        session, draft=draft, user_id=user_id
    )
    append_event(
        session,
        run,
        "review.cards_created",
        {
            "card_count": len(review_cards),
            "due_immediately": True,
            "scheduler_version": FSRS_SCHEDULER_VERSION,
        },
    )
    candidate = MemoryCandidate(
        user_id=user_id,
        run_id=run.id,
        kind="goal",
        canonical_key="active_learning_goal",
        content=draft.learning_goal,
        rationale="用户确认了围绕该目标生成的知识草稿，可作为后续个性化的候选记忆。",
        importance=0.65,
        confidence=0.8,
        evidence_json=[{"type": "confirmed_draft", "draft_id": str(draft.id)}],
        status="pending",
    )
    session.add(candidate)
    session.flush()
    append_event(
        session,
        run,
        "memory.candidate_created",
        {
            "candidate_id": str(candidate.id),
            "kind": candidate.kind,
            "requires_separate_approval": True,
        },
    )
    run.stop_reason = "draft_confirmed_memory_pending"
    transition_run(session, run, RunState.COMPLETED)
    session.commit()
    return draft, candidate


def decide_memory_candidate(
    session: Session,
    *,
    candidate_id: UUID,
    user_id: UUID,
    decision: str,
) -> MemoryCandidate:
    candidate = session.scalar(
        select(MemoryCandidate).where(
            MemoryCandidate.id == candidate_id,
            MemoryCandidate.user_id == user_id,
        )
    )
    if candidate is None:
        raise LookupError("memory candidate not found")
    if candidate.status != "pending":
        return candidate
    candidate.status = "approved" if decision == "approve" else "rejected"
    candidate.reviewed_at = datetime.now(UTC)
    if decision == "approve":
        run = session.get(AgentRun, candidate.run_id)
        if run is None:
            raise RuntimeError("candidate run not found")
        previous = session.scalar(
            select(MemoryItem)
            .where(
                MemoryItem.user_id == user_id,
                MemoryItem.profile_id == run.profile_id,
                MemoryItem.canonical_key == candidate.canonical_key,
                MemoryItem.status == "active",
            )
            .order_by(MemoryItem.version.desc())
        )
        if previous is not None:
            previous.status = "superseded"
            previous.valid_to = datetime.now(UTC)
        item = MemoryItem(
            user_id=user_id,
            profile_id=run.profile_id,
            kind=candidate.kind,
            scope_type="user",
            canonical_key=candidate.canonical_key,
            content=candidate.content,
            compact_summary=candidate.content[:300],
            importance=candidate.importance,
            confidence=candidate.confidence,
            status="active",
            version=(previous.version + 1) if previous else 1,
            supersedes_id=previous.id if previous else None,
            valid_from=datetime.now(UTC),
            content_hash=_sha256(candidate.content),
        )
        session.add(item)
        session.flush()
        session.add(
            MemoryEvidence(
                memory_id=item.id,
                evidence_type="memory_candidate",
                evidence_ref=str(candidate.id),
                excerpt=candidate.rationale,
            )
        )
        normalized = " ".join(candidate.content.lower().split())
        keywords = sorted(
            {
                token.lower()
                for token in re.findall(
                    r"[A-Za-z0-9_\-]+|[\u4e00-\u9fff]{2,}", candidate.content
                )
                if len(token) > 1
            }
        )[:50]
        session.add(
            RetrievalDocument(
                user_id=user_id,
                document_type="memory",
                owner_id=str(item.id),
                owner_version=str(item.version),
                scope_type=item.scope_type,
                scope_id=item.scope_id,
                content=item.content,
                normalized_content=normalized,
                keywords=keywords,
                document_metadata={
                    "kind": item.kind,
                    "canonical_key": item.canonical_key,
                    "memory_status": item.status,
                },
                embedding_vector=None,
                content_hash=item.content_hash,
                active=True,
            )
        )
    session.commit()
    return candidate
