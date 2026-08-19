from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, func, select, update
from sqlalchemy.orm import Session, selectinload

from memory_agent.models import ConversationTurn, DraftUnit, KnowledgeDraft, ReviewCard, Source


def _source_payload(source: Source, *, from_conversation: bool) -> dict[str, Any]:
    return {
        "id": source.id,
        "title": source.title,
        "origin_type": source.origin_type,
        "origin_url": source.origin_url,
        "context_type": (
            "url" if source.origin_type == "url" else
            "conversation" if from_conversation else
            "direct_input"
        ),
    }


def _title(draft: KnowledgeDraft, source: Source, first_unit: DraftUnit | None = None) -> str:
    return (draft.title or source.title or (first_unit.title if first_unit else None) or "未命名知识集").strip()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def list_knowledge_sets(session: Session, *, user_id: UUID) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    active_unit = and_(DraftUnit.draft_id == KnowledgeDraft.id, DraftUnit.status == "active")
    rows = session.execute(
        select(
            KnowledgeDraft,
            Source,
            func.count(DraftUnit.id).label("unit_count"),
            func.coalesce(func.sum(ReviewCard.review_count), 0).label("review_count"),
            func.max(ReviewCard.last_reviewed_at).label("last_reviewed_at"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                ReviewCard.status == "active",
                                ReviewCard.due_at <= now,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("due_count"),
        )
        .join(Source, KnowledgeDraft.source_id == Source.id)
        .outerjoin(DraftUnit, active_unit)
        .outerjoin(
            ReviewCard,
            and_(
                ReviewCard.draft_unit_id == DraftUnit.id,
                ReviewCard.user_id == user_id,
            ),
        )
        .where(
            KnowledgeDraft.user_id == user_id,
            KnowledgeDraft.status == "confirmed",
        )
        .group_by(KnowledgeDraft.id, Source.id)
        .having(func.count(DraftUnit.id) > 0)
        .order_by(KnowledgeDraft.updated_at.desc(), KnowledgeDraft.created_at.desc())
    ).all()
    conversation_source_ids = set(
        session.scalars(
            select(ConversationTurn.source_id)
            .where(ConversationTurn.source_id.in_([source.id for _, source, *_ in rows]))
            .distinct()
        ).all()
    ) if rows else set()
    return [
        {
            "id": draft.id,
            "title": _title(draft, source),
            "unit_count": int(unit_count or 0),
            "due_count": int(due_count or 0),
            "review_count": int(review_count or 0),
            "last_reviewed_at": last_reviewed_at,
            "source": _source_payload(
                source, from_conversation=source.id in conversation_source_ids
            ),
            "created_at": draft.confirmed_at or draft.created_at,
            "updated_at": draft.updated_at,
        }
        for draft, source, unit_count, review_count, last_reviewed_at, due_count in rows
    ]


def get_knowledge_set(
    session: Session, *, knowledge_set_id: UUID, user_id: UUID
) -> dict[str, Any]:
    bundle = session.execute(
        select(KnowledgeDraft, Source)
        .join(Source, KnowledgeDraft.source_id == Source.id)
        .where(
            KnowledgeDraft.id == knowledge_set_id,
            KnowledgeDraft.user_id == user_id,
            KnowledgeDraft.status == "confirmed",
        )
    ).one_or_none()
    if bundle is None:
        raise LookupError("knowledge set not found")
    draft, source = bundle
    from_conversation = session.scalar(
        select(func.count(ConversationTurn.id)).where(ConversationTurn.source_id == source.id)
    )
    rows = session.execute(
        select(DraftUnit, ReviewCard)
        .outerjoin(
            ReviewCard,
            and_(
                ReviewCard.draft_unit_id == DraftUnit.id,
                ReviewCard.user_id == user_id,
            ),
        )
        .where(DraftUnit.draft_id == draft.id, DraftUnit.status == "active")
        .options(selectinload(DraftUnit.evidence))
        .order_by(DraftUnit.position)
    ).all()
    units = []
    review_count = 0
    due_count = 0
    last_reviewed_at = None
    now = datetime.now(UTC)
    for unit, card in rows:
        if card is not None:
            review_count += card.review_count
            if card.status == "active" and _as_utc(card.due_at) <= now:
                due_count += 1
            if card.last_reviewed_at and (
                last_reviewed_at is None or card.last_reviewed_at > last_reviewed_at
            ):
                last_reviewed_at = card.last_reviewed_at
        units.append(
            {
                "id": unit.id,
                "position": unit.position,
                "title": unit.title,
                "question": unit.question,
                "answer": "\n".join(unit.answer_key),
                "explanation": unit.explanation,
                "evidence": list(unit.evidence),
                "review_count": card.review_count if card else 0,
                "last_reviewed_at": card.last_reviewed_at if card else None,
            }
        )
    return {
        "id": draft.id,
        "title": _title(draft, source, rows[0][0] if rows else None),
        "unit_count": len(units),
        "due_count": due_count,
        "review_count": review_count,
        "last_reviewed_at": last_reviewed_at,
        "source": _source_payload(source, from_conversation=bool(from_conversation)),
        "created_at": draft.confirmed_at or draft.created_at,
        "updated_at": draft.updated_at,
        "learning_goal": draft.learning_goal,
        "units": units,
    }


def rename_knowledge_set(
    session: Session, *, knowledge_set_id: UUID, user_id: UUID, title: str
) -> dict[str, Any]:
    draft = session.scalar(
        select(KnowledgeDraft).where(
            KnowledgeDraft.id == knowledge_set_id,
            KnowledgeDraft.user_id == user_id,
            KnowledgeDraft.status == "confirmed",
        )
    )
    if draft is None:
        raise LookupError("knowledge set not found")
    draft.title = title.strip()
    session.commit()
    return get_knowledge_set(session, knowledge_set_id=knowledge_set_id, user_id=user_id)


def update_knowledge_unit(
    session: Session, *, unit_id: UUID, user_id: UUID, question: str, answer: str
) -> dict[str, Any]:
    row = session.execute(
        select(DraftUnit, KnowledgeDraft)
        .join(KnowledgeDraft, DraftUnit.draft_id == KnowledgeDraft.id)
        .where(
            DraftUnit.id == unit_id,
            DraftUnit.status == "active",
            KnowledgeDraft.user_id == user_id,
            KnowledgeDraft.status == "confirmed",
        )
    ).one_or_none()
    if row is None:
        raise LookupError("knowledge unit not found")
    unit, draft = row
    unit.question = question.strip()
    unit.answer_key = [line.strip() for line in answer.splitlines() if line.strip()]
    session.commit()
    return get_knowledge_set(session, knowledge_set_id=draft.id, user_id=user_id)


def archive_knowledge_unit(session: Session, *, unit_id: UUID, user_id: UUID) -> UUID:
    row = session.execute(
        select(DraftUnit, KnowledgeDraft)
        .join(KnowledgeDraft, DraftUnit.draft_id == KnowledgeDraft.id)
        .where(
            DraftUnit.id == unit_id,
            DraftUnit.status == "active",
            KnowledgeDraft.user_id == user_id,
            KnowledgeDraft.status == "confirmed",
        )
    ).one_or_none()
    if row is None:
        raise LookupError("knowledge unit not found")
    unit, draft = row
    unit.status = "archived"
    session.execute(
        update(ReviewCard)
        .where(ReviewCard.user_id == user_id, ReviewCard.draft_unit_id == unit.id)
        .values(status="archived")
    )
    remaining = session.scalar(
        select(func.count(DraftUnit.id)).where(
            DraftUnit.draft_id == draft.id,
            DraftUnit.status == "active",
            DraftUnit.id != unit.id,
        )
    )
    if not remaining:
        draft.status = "archived"
    session.commit()
    return draft.id


def archive_knowledge_set(
    session: Session, *, knowledge_set_id: UUID, user_id: UUID
) -> None:
    draft = session.scalar(
        select(KnowledgeDraft).where(
            KnowledgeDraft.id == knowledge_set_id,
            KnowledgeDraft.user_id == user_id,
            KnowledgeDraft.status == "confirmed",
        )
    )
    if draft is None:
        raise LookupError("knowledge set not found")
    unit_ids = select(DraftUnit.id).where(DraftUnit.draft_id == draft.id)
    session.execute(
        update(ReviewCard)
        .where(ReviewCard.user_id == user_id, ReviewCard.draft_unit_id.in_(unit_ids))
        .values(status="archived")
    )
    session.execute(
        update(DraftUnit).where(DraftUnit.draft_id == draft.id).values(status="archived")
    )
    draft.status = "archived"
    session.commit()
