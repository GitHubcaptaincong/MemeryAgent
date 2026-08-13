from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory_agent.answer_evaluator import AnswerEvaluationInput, build_answer_evaluator
from memory_agent.config import Settings
from memory_agent.models import DraftUnit, ReviewCard, ReviewEvent


class AnswerEvaluationProcessor:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.evaluator = build_answer_evaluator(settings)

    def execute(self, *, user_id: UUID, card_id: UUID, attempt_id: UUID) -> None:
        session = self.session_factory()
        try:
            if _result_event(session, user_id=user_id, card_id=card_id, attempt_id=attempt_id):
                return
            row = session.execute(
                select(ReviewEvent, ReviewCard, DraftUnit)
                .join(ReviewCard, ReviewEvent.card_id == ReviewCard.id)
                .join(DraftUnit, ReviewCard.draft_unit_id == DraftUnit.id)
                .where(
                    ReviewEvent.user_id == user_id,
                    ReviewEvent.card_id == card_id,
                    ReviewEvent.correlation_id == attempt_id,
                    ReviewEvent.event_type == "answer_submitted",
                )
            ).one_or_none()
            if row is None:
                raise LookupError("review answer attempt not found")
            answer_event, _card, unit = row
            result = self.evaluator.evaluate(
                AnswerEvaluationInput(
                    question=unit.question,
                    answer_key=[str(item) for item in unit.answer_key],
                    answer=str(answer_event.payload_json.get("answer") or ""),
                )
            )
            payload = result.to_payload()
            payload["schema_version"] = 1
            payload["evaluated_at"] = datetime.now(UTC).isoformat()
            session.add(
                ReviewEvent(
                    user_id=user_id,
                    card_id=card_id,
                    correlation_id=attempt_id,
                    event_type="answer_evaluation_completed",
                    idempotency_key=f"evaluation-completed-{attempt_id}",
                    payload_json=payload,
                )
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            if _result_event(session, user_id=user_id, card_id=card_id, attempt_id=attempt_id):
                return
            raise
        finally:
            session.close()

    def record_failed(
        self,
        *,
        user_id: UUID,
        card_id: UUID,
        attempt_id: UUID,
        error_code: str,
        retryable: bool,
    ) -> None:
        session = self.session_factory()
        try:
            if _result_event(session, user_id=user_id, card_id=card_id, attempt_id=attempt_id):
                return
            session.add(
                ReviewEvent(
                    user_id=user_id,
                    card_id=card_id,
                    correlation_id=attempt_id,
                    event_type="answer_evaluation_failed",
                    idempotency_key=f"evaluation-failed-{attempt_id}",
                    payload_json={
                        "schema_version": 1,
                        "error_code": error_code,
                        "retryable": retryable,
                        "evaluated_at": datetime.now(UTC).isoformat(),
                    },
                )
            )
            session.commit()
        except IntegrityError:
            session.rollback()
        finally:
            session.close()


def _result_event(
    session: Session, *, user_id: UUID, card_id: UUID, attempt_id: UUID
) -> ReviewEvent | None:
    return session.scalar(
        select(ReviewEvent).where(
            ReviewEvent.user_id == user_id,
            ReviewEvent.card_id == card_id,
            ReviewEvent.correlation_id == attempt_id,
            ReviewEvent.event_type.in_(
                ("answer_evaluation_completed", "answer_evaluation_failed")
            ),
        )
    )
