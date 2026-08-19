from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory_agent.models import (
    DraftSourceSpan,
    DraftUnit,
    KnowledgeDraft,
    ReminderPreference,
    BackgroundJob,
    ReviewCard,
    ReviewEvent,
    Source,
    utc_now,
)
from memory_agent.scheduler import (
    FsrsReviewScheduler,
    HistoricalReview,
    SchedulerAdapter,
)


def create_review_cards_for_draft(
    session: Session, *, draft: KnowledgeDraft, user_id: UUID
) -> list[ReviewCard]:
    cards: list[ReviewCard] = []
    scheduler = FsrsReviewScheduler()
    for unit in draft.units:
        if unit.status != "active":
            continue
        existing = session.scalar(
            select(ReviewCard).where(
                ReviewCard.user_id == user_id,
                ReviewCard.draft_unit_id == unit.id,
            )
        )
        if existing is not None:
            cards.append(existing)
            continue
        card_id = uuid.uuid4()
        due_at = utc_now()
        card = ReviewCard(
            id=card_id,
            user_id=user_id,
            draft_unit_id=unit.id,
            status="active",
            due_at=due_at,
            interval_days=0.0,
            review_count=0,
            lapse_count=0,
            scheduler_version=scheduler.version,
            scheduler_state=scheduler.initial_state(card_id=card_id, due_at=due_at),
        )
        session.add(card)
        cards.append(card)
    session.flush()
    return cards


def list_due_review_cards(
    session: Session,
    *,
    user_id: UUID,
    limit: int,
    now: datetime | None = None,
    knowledge_set_id: UUID | None = None,
) -> list[dict[str, Any]]:
    due = now or datetime.now(UTC)
    scheduler = FsrsReviewScheduler()
    statement = (
        select(ReviewCard, DraftUnit, KnowledgeDraft, Source)
        .join(DraftUnit, ReviewCard.draft_unit_id == DraftUnit.id)
        .join(KnowledgeDraft, DraftUnit.draft_id == KnowledgeDraft.id)
        .join(Source, KnowledgeDraft.source_id == Source.id)
        .where(
            ReviewCard.user_id == user_id,
            ReviewCard.status == "active",
            ReviewCard.due_at <= due,
            DraftUnit.status == "active",
            KnowledgeDraft.status == "confirmed",
        )
        .order_by(ReviewCard.due_at, ReviewCard.created_at)
        .limit(limit)
    )
    if knowledge_set_id is not None:
        statement = statement.where(KnowledgeDraft.id == knowledge_set_id)
    rows = session.execute(statement).all()
    return [
        _card_payload(
            card,
            unit,
            draft,
            source,
            rating_options=_rating_options(
                scheduler,
                card,
                reviewed_at=due,
                history=_historical_reviews(session, card_id=card.id),
            ),
        )
        for card, unit, draft, source in rows
    ]


def get_review_overview(
    session: Session, *, user_id: UUID, now: datetime | None = None
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    active_filter = (
        ReviewCard.user_id == user_id,
        ReviewCard.status == "active",
        DraftUnit.status == "active",
        KnowledgeDraft.status == "confirmed",
    )
    active_cards = (
        select(ReviewCard)
        .join(DraftUnit, ReviewCard.draft_unit_id == DraftUnit.id)
        .join(KnowledgeDraft, DraftUnit.draft_id == KnowledgeDraft.id)
    )
    due_count = session.scalar(
        active_cards.with_only_columns(func.count(ReviewCard.id)).where(
            *active_filter,
            ReviewCard.due_at <= current,
        )
    )
    total_active = session.scalar(
        active_cards.with_only_columns(func.count(ReviewCard.id)).where(*active_filter)
    )
    next_due_at = session.scalar(
        active_cards.with_only_columns(func.min(ReviewCard.due_at)).where(
            *active_filter,
            ReviewCard.due_at > current,
        )
    )
    return {
        "due_count": int(due_count or 0),
        "total_active": int(total_active or 0),
        "next_due_at": next_due_at,
    }


def list_review_history(
    session: Session, *, user_id: UUID, limit: int
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(ReviewEvent, ReviewCard, DraftUnit, KnowledgeDraft, Source)
        .join(ReviewCard, ReviewEvent.card_id == ReviewCard.id)
        .join(DraftUnit, ReviewCard.draft_unit_id == DraftUnit.id)
        .join(KnowledgeDraft, DraftUnit.draft_id == KnowledgeDraft.id)
        .join(Source, KnowledgeDraft.source_id == Source.id)
        .where(
            ReviewEvent.user_id == user_id,
            ReviewEvent.event_type == "review_rated",
        )
        .order_by(ReviewEvent.created_at.desc(), ReviewEvent.id.desc())
        .limit(limit)
    ).all()
    history: list[dict[str, Any]] = []
    for event, card, unit, _draft, source in rows:
        payload = event.payload_json or {}
        history.append(
            {
                "id": event.id,
                "card_id": card.id,
                "title": unit.title,
                "question": unit.question,
                "source_title": source.title,
                "rating": payload.get("rating"),
                "reviewed_at": payload.get("reviewed_at") or event.created_at,
                "next_due_at": payload.get("next_due_at") or card.due_at,
                "interval_days": payload.get("interval_days", card.interval_days),
                "scheduler_version": payload.get(
                    "scheduler_version", card.scheduler_version
                ),
                "user_rating_is_final": payload.get(
                    "user_rating_is_final", True
                ),
            }
        )
    return history


def submit_review_answer(
    session: Session,
    *,
    user_id: UUID,
    card_id: UUID,
    answer: str,
    idempotency_key: str,
    evaluation_enabled: bool | None = None,
) -> tuple[dict[str, Any], BackgroundJob | None]:
    existing = session.scalar(
        select(ReviewEvent).where(
            ReviewEvent.user_id == user_id,
            ReviewEvent.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.event_type != "answer_submitted" or existing.card_id != card_id:
            raise ValueError("idempotency key is already used by another review event")
        if existing.payload_json.get("answer") != answer.strip():
            raise ValueError("idempotency key was already used with another answer")
        _card, unit, _draft, _source = _card_bundle(
            session, user_id=user_id, card_id=card_id
        )
        return _answer_payload(session, existing, unit), _evaluation_job_for_attempt(
            session, user_id=user_id, attempt_id=existing.correlation_id
        )

    card, unit, draft, _source = _card_bundle(
        session, user_id=user_id, card_id=card_id, lock=True
    )
    concurrent_existing = session.scalar(
        select(ReviewEvent).where(
            ReviewEvent.user_id == user_id,
            ReviewEvent.idempotency_key == idempotency_key,
        )
    )
    if concurrent_existing is not None:
        if (
            concurrent_existing.event_type != "answer_submitted"
            or concurrent_existing.card_id != card_id
            or concurrent_existing.payload_json.get("answer") != answer.strip()
        ):
            raise ValueError("idempotency key is already used by another review event")
        return _answer_payload(session, concurrent_existing, unit), _evaluation_job_for_attempt(
            session, user_id=user_id, attempt_id=concurrent_existing.correlation_id
        )
    if card.status != "active":
        raise ValueError("review card is not active")
    if _as_utc(card.due_at) > datetime.now(UTC):
        raise ValueError("review card is not due yet")

    enabled = (
        get_reminder_preference(session, user_id=user_id).ai_evaluation_enabled
        if evaluation_enabled is None
        else evaluation_enabled
    )
    attempt_id = uuid.uuid4()
    event = ReviewEvent(
        user_id=user_id,
        card_id=card.id,
        correlation_id=attempt_id,
        event_type="answer_submitted",
        idempotency_key=idempotency_key,
        payload_json={
            "schema_version": 1,
            "answer": answer.strip(),
            "evaluation_status": "pending" if enabled else "disabled",
            "review_count_at_submission": card.review_count,
            "due_at_at_submission": _as_utc(card.due_at).isoformat(),
        },
        created_at=utc_now(),
    )
    session.add(event)
    job = None
    if enabled:
        job = BackgroundJob(
            id=attempt_id,
            run_id=draft.run_id,
            job_type="review_answer_evaluation",
            status="queued",
            priority=50,
            max_attempts=2,
            payload_json={
                "user_id": str(user_id),
                "card_id": str(card.id),
                "attempt_id": str(attempt_id),
            },
        )
        session.add(job)
    session.commit()
    return _answer_payload(session, event, unit), job


def get_answer_evaluation(
    session: Session, *, user_id: UUID, card_id: UUID, attempt_id: UUID
) -> dict[str, Any]:
    answer_event = session.scalar(
        select(ReviewEvent).where(
            ReviewEvent.user_id == user_id,
            ReviewEvent.card_id == card_id,
            ReviewEvent.correlation_id == attempt_id,
            ReviewEvent.event_type == "answer_submitted",
        )
    )
    if answer_event is None:
        raise LookupError("review answer attempt not found")
    completed = _evaluation_event(
        session, user_id=user_id, card_id=card_id, attempt_id=attempt_id
    )
    status_value = answer_event.payload_json.get("evaluation_status", "disabled")
    if completed is not None:
        status_value = "completed" if completed.event_type.endswith("completed") else "failed"
    return {
        "attempt_id": attempt_id,
        "card_id": card_id,
        "status": status_value,
        "evaluation": completed.payload_json if completed and status_value == "completed" else None,
    }


def get_answer_evaluation_job(
    session: Session, *, user_id: UUID, attempt_id: UUID
) -> BackgroundJob | None:
    return _evaluation_job_for_attempt(
        session, user_id=user_id, attempt_id=attempt_id
    )


def rate_review_answer(
    session: Session,
    *,
    user_id: UUID,
    card_id: UUID,
    attempt_id: UUID,
    rating: int,
    idempotency_key: str,
    scheduler: SchedulerAdapter | None = None,
) -> dict[str, Any]:
    existing_key = session.scalar(
        select(ReviewEvent).where(
            ReviewEvent.user_id == user_id,
            ReviewEvent.idempotency_key == idempotency_key,
        )
    )
    if existing_key is not None:
        if existing_key.event_type != "review_rated" or existing_key.card_id != card_id:
            raise ValueError("idempotency key is already used by another review event")
        payload = existing_key.payload_json
        if payload.get("attempt_id") != str(attempt_id) or payload.get("rating") != rating:
            raise ValueError("idempotency key was already used with another rating")
        return existing_key.payload_json

    card, _unit, _draft, _source = _card_bundle(
        session, user_id=user_id, card_id=card_id, lock=True
    )
    concurrent_existing = session.scalar(
        select(ReviewEvent).where(
            ReviewEvent.user_id == user_id,
            ReviewEvent.idempotency_key == idempotency_key,
        )
    )
    if concurrent_existing is not None:
        payload = concurrent_existing.payload_json
        if (
            concurrent_existing.event_type != "review_rated"
            or concurrent_existing.card_id != card_id
            or payload.get("attempt_id") != str(attempt_id)
            or payload.get("rating") != rating
        ):
            raise ValueError("idempotency key is already used by another review event")
        return payload
    if card.status != "active":
        raise ValueError("review card is not active")
    answer_event = session.scalar(
        select(ReviewEvent).where(
            ReviewEvent.user_id == user_id,
            ReviewEvent.card_id == card_id,
            ReviewEvent.correlation_id == attempt_id,
            ReviewEvent.event_type == "answer_submitted",
        )
    )
    if answer_event is None:
        raise LookupError("review answer attempt not found")
    evaluation_event = _evaluation_event(
        session, user_id=user_id, card_id=card_id, attempt_id=attempt_id
    )
    previous_rating = session.scalar(
        select(ReviewEvent).where(
            ReviewEvent.user_id == user_id,
            ReviewEvent.card_id == card_id,
            ReviewEvent.correlation_id == attempt_id,
            ReviewEvent.event_type == "review_rated",
        )
    )
    if previous_rating is not None:
        if previous_rating.payload_json.get("rating") != rating:
            raise ValueError("review attempt was already rated differently")
        return previous_rating.payload_json

    submitted_review_count = answer_event.payload_json.get(
        "review_count_at_submission"
    )
    if (
        submitted_review_count is not None
        and int(submitted_review_count) != card.review_count
    ):
        raise ValueError("review answer attempt is stale")

    reviewed_at = datetime.now(UTC)
    result = (scheduler or FsrsReviewScheduler()).schedule(
        card,
        rating=rating,
        reviewed_at=reviewed_at,
        history=_historical_reviews(session, card_id=card.id),
    )
    before = {
        "due_at": _as_utc(card.due_at).isoformat(),
        "interval_days": card.interval_days,
        "review_count": card.review_count,
        "lapse_count": card.lapse_count,
        "scheduler_version": card.scheduler_version,
        "scheduler_state": card.scheduler_state,
    }
    card.due_at = result.due_at
    card.interval_days = result.interval_days
    card.review_count += 1
    card.lapse_count += result.lapse_delta
    card.last_reviewed_at = reviewed_at
    card.scheduler_version = result.scheduler_version
    card.scheduler_state = result.scheduler_state
    payload = {
        "schema_version": 1,
        "card_id": str(card.id),
        "attempt_id": str(attempt_id),
        "rating": rating,
        "reviewed_at": reviewed_at.isoformat(),
        "next_due_at": result.due_at.isoformat(),
        "interval_days": result.interval_days,
        "review_count": card.review_count,
        "lapse_count": card.lapse_count,
        "scheduler_version": result.scheduler_version,
        "scheduler_state": result.scheduler_state,
        "scheduler_config_version": result.scheduler_state.get("config_version"),
        "scheduler_card_before": result.scheduler_card_before,
        "scheduler_card_after": result.scheduler_card_after,
        "review_log": result.review_log,
        "schedule_before": before,
        "user_rating_is_final": True,
        "ai_suggested_rating": (
            evaluation_event.payload_json.get("suggested_rating")
            if evaluation_event and evaluation_event.event_type.endswith("completed")
            else None
        ),
    }
    payload["user_overrode_ai"] = (
        payload["ai_suggested_rating"] is not None
        and payload["ai_suggested_rating"] != rating
    )
    session.add(
        ReviewEvent(
            user_id=user_id,
            card_id=card.id,
            correlation_id=attempt_id,
            event_type="review_rated",
            idempotency_key=idempotency_key,
            payload_json=payload,
            created_at=reviewed_at,
        )
    )
    session.commit()
    return payload


def get_reminder_preference(session: Session, *, user_id: UUID) -> ReminderPreference:
    preference = session.scalar(
        select(ReminderPreference).where(ReminderPreference.user_id == user_id)
    )
    if preference is None:
        preference = ReminderPreference(user_id=user_id)
        session.add(preference)
        session.commit()
    return preference


def update_reminder_preference(
    session: Session,
    *,
    user_id: UUID,
    enabled: bool,
    preferred_time: str,
    daily_limit: int,
    overdue_enabled: bool,
    ai_evaluation_enabled: bool,
    timezone: str,
) -> ReminderPreference:
    preference = get_reminder_preference(session, user_id=user_id)
    preference.enabled = enabled
    preference.preferred_time = preferred_time
    preference.daily_limit = daily_limit
    preference.overdue_enabled = overdue_enabled
    preference.ai_evaluation_enabled = ai_evaluation_enabled
    preference.timezone = timezone
    session.commit()
    return preference


def _card_bundle(
    session: Session, *, user_id: UUID, card_id: UUID, lock: bool = False
) -> tuple[ReviewCard, DraftUnit, KnowledgeDraft, Source]:
    statement = (
        select(ReviewCard, DraftUnit, KnowledgeDraft, Source)
        .join(DraftUnit, ReviewCard.draft_unit_id == DraftUnit.id)
        .join(KnowledgeDraft, DraftUnit.draft_id == KnowledgeDraft.id)
        .join(Source, KnowledgeDraft.source_id == Source.id)
        .where(ReviewCard.id == card_id, ReviewCard.user_id == user_id)
    )
    if lock:
        statement = statement.with_for_update(of=ReviewCard)
    row = session.execute(statement).one_or_none()
    if row is None:
        raise LookupError("review card not found")
    return row


def _card_payload(
    card: ReviewCard,
    unit: DraftUnit,
    draft: KnowledgeDraft,
    source: Source,
    *,
    rating_options: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": card.id,
        "draft_unit_id": unit.id,
        "title": unit.title,
        "question": unit.question,
        "hints": unit.hints,
        "source_title": source.title,
        "due_at": card.due_at,
        "interval_days": card.interval_days,
        "review_count": card.review_count,
        "lapse_count": card.lapse_count,
        "scheduler_version": card.scheduler_version,
        "learning_goal": draft.learning_goal,
        "rating_options": rating_options,
    }


def _answer_payload(
    session: Session, event: ReviewEvent, unit: DraftUnit
) -> dict[str, Any]:
    evidence = session.scalars(
        select(DraftSourceSpan).where(DraftSourceSpan.unit_id == unit.id)
    ).all()
    evaluation_event = _evaluation_event(
        session,
        user_id=event.user_id,
        card_id=event.card_id,
        attempt_id=event.correlation_id,
    )
    evaluation_status = event.payload_json.get("evaluation_status", "disabled")
    if evaluation_event is not None:
        evaluation_status = (
            "completed" if evaluation_event.event_type.endswith("completed") else "failed"
        )
    return {
        "attempt_id": event.correlation_id,
        "card_id": event.card_id,
        "answer": event.payload_json.get("answer", ""),
        "answer_key": unit.answer_key,
        "evidence": [
            {
                "evidence_type": item.evidence_type,
                "source_id": item.source_id,
                "start_char": item.start_char,
                "end_char": item.end_char,
                "quote": item.quote,
                "url": item.url,
            }
            for item in evidence
        ],
        "evaluation_status": evaluation_status,
        "evaluation": (
            evaluation_event.payload_json
            if evaluation_event and evaluation_status == "completed"
            else None
        ),
        "submitted_at": event.created_at,
    }


def _evaluation_event(
    session: Session, *, user_id: UUID, card_id: UUID, attempt_id: UUID
) -> ReviewEvent | None:
    return session.scalar(
        select(ReviewEvent)
        .where(
            ReviewEvent.user_id == user_id,
            ReviewEvent.card_id == card_id,
            ReviewEvent.correlation_id == attempt_id,
            ReviewEvent.event_type.in_(
                ("answer_evaluation_completed", "answer_evaluation_failed")
            ),
        )
        .order_by(ReviewEvent.created_at.desc())
    )


def _evaluation_job_for_attempt(
    session: Session, *, user_id: UUID, attempt_id: UUID
) -> BackgroundJob | None:
    job = session.get(BackgroundJob, attempt_id)
    if job is None or job.job_type != "review_answer_evaluation":
        return None
    payload = job.payload_json or {}
    if payload.get("user_id") != str(user_id) or payload.get("attempt_id") != str(
        attempt_id
    ):
        return None
    return job


def _rating_options(
    scheduler: SchedulerAdapter,
    card: ReviewCard,
    *,
    reviewed_at: datetime,
    history: list[HistoricalReview],
) -> list[dict[str, Any]]:
    return [
        {
            "rating": rating,
            "due_at": result.due_at,
            "interval_days": result.interval_days,
        }
        for rating in (1, 2, 3, 4)
        for result in [
            scheduler.schedule(
                card,
                rating=rating,
                reviewed_at=reviewed_at,
                history=history,
            )
        ]
    ]


def _historical_reviews(
    session: Session, *, card_id: UUID
) -> list[HistoricalReview]:
    events = session.scalars(
        select(ReviewEvent)
        .where(
            ReviewEvent.card_id == card_id,
            ReviewEvent.event_type == "review_rated",
        )
        .order_by(ReviewEvent.created_at, ReviewEvent.id)
    ).all()
    history: list[HistoricalReview] = []
    for event in events:
        payload = event.payload_json or {}
        rating = payload.get("rating")
        reviewed_at = payload.get("reviewed_at") or event.created_at
        if rating is None:
            raise ValueError("review history is missing a rating")
        history.append(
            HistoricalReview(
                rating=int(rating),
                reviewed_at=_parse_datetime(reviewed_at),
            )
        )
    return history


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str):
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    raise ValueError("review history contains an invalid datetime")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
