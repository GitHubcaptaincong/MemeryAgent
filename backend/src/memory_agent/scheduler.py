from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, Sequence

from fsrs import Card, Rating, Scheduler, State

from memory_agent.models import ReviewCard
from memory_agent.review_config import (
    FSRS_CONFIG_VERSION,
    FSRS_LIBRARY_VERSION,
    FSRS_SCHEDULER_VERSION,
)


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    rating: int
    due_at: datetime
    interval_days: float
    lapse_delta: int
    scheduler_version: str
    scheduler_state: dict[str, Any]
    review_log: dict[str, Any] | None = None
    scheduler_card_before: dict[str, Any] | None = None
    scheduler_card_after: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class HistoricalReview:
    rating: int
    reviewed_at: datetime


class SchedulerAdapter(Protocol):
    version: str

    def schedule(
        self,
        card: ReviewCard,
        *,
        rating: int,
        reviewed_at: datetime | None = None,
        history: Sequence[HistoricalReview] = (),
    ) -> ScheduleResult: ...


class FsrsReviewScheduler:
    """Deterministic FSRS v6 adapter with auditable, versioned card state."""

    version = FSRS_SCHEDULER_VERSION

    def __init__(self) -> None:
        # Keep the official parameters and learning steps. Fuzzing is disabled in
        # config v1 so an immutable review history can be replayed exactly.
        self._scheduler = Scheduler(enable_fuzzing=False)

    def initial_state(self, *, card_id: Any, due_at: datetime) -> dict[str, Any]:
        fsrs_card = Card(card_id=_card_id_as_int(card_id), due=_as_utc(due_at))
        return self._state_payload(fsrs_card)

    def schedule(
        self,
        card: ReviewCard,
        *,
        rating: int,
        reviewed_at: datetime | None = None,
        history: Sequence[HistoricalReview] = (),
    ) -> ScheduleResult:
        if rating not in {1, 2, 3, 4}:
            raise ValueError("rating must be between 1 and 4")

        now = _as_utc(reviewed_at or datetime.now(UTC))
        fsrs_card, migration = self._load_or_replay(card, history=history)
        before = fsrs_card.to_dict()
        previous_state = fsrs_card.state
        updated_card, review_log = self._scheduler.review_card(
            fsrs_card,
            Rating(rating),
            review_datetime=now,
        )
        interval_days = round(
            max(0.0, (updated_card.due - now).total_seconds() / 86_400),
            6,
        )
        return ScheduleResult(
            rating=rating,
            due_at=updated_card.due,
            interval_days=interval_days,
            lapse_delta=int(rating == Rating.Again and previous_state == State.Review),
            scheduler_version=self.version,
            scheduler_state=self._state_payload(updated_card, migration=migration),
            review_log=review_log.to_dict(),
            scheduler_card_before=before,
            scheduler_card_after=updated_card.to_dict(),
        )

    def _load_or_replay(
        self, card: ReviewCard, *, history: Sequence[HistoricalReview]
    ) -> tuple[Card, dict[str, Any] | None]:
        state = card.scheduler_state or {}
        if (
            state.get("algorithm") == "fsrs-6"
            and state.get("config_version") == FSRS_CONFIG_VERSION
            and state.get("library_version") == FSRS_LIBRARY_VERSION
            and isinstance(state.get("card"), dict)
        ):
            fsrs_card = Card.from_dict(state["card"])
            if fsrs_card.card_id != _card_id_as_int(card.id):
                raise ValueError("stored FSRS card id does not match review card")
            return fsrs_card, None

        if len(history) != int(card.review_count or 0):
            raise ValueError(
                "legacy review history is incomplete; FSRS migration was not applied"
            )

        ordered_history = sorted(history, key=lambda item: _as_utc(item.reviewed_at))
        initial_due = (
            _as_utc(ordered_history[0].reviewed_at)
            if ordered_history
            else _as_utc(card.due_at)
        )
        replayed_card = Card(card_id=_card_id_as_int(card.id), due=initial_due)
        for item in ordered_history:
            if item.rating not in {1, 2, 3, 4}:
                raise ValueError("legacy review history contains an invalid rating")
            replayed_card, _ = self._scheduler.review_card(
                replayed_card,
                Rating(item.rating),
                review_datetime=_as_utc(item.reviewed_at),
            )
        migration = {
            "from_scheduler_version": card.scheduler_version,
            "from_config_version": state.get("config_version"),
            "replayed_review_count": len(ordered_history),
        }
        return replayed_card, migration

    @staticmethod
    def _state_payload(
        card: Card, *, migration: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "algorithm": "fsrs-6",
            "library": "fsrs",
            "library_version": FSRS_LIBRARY_VERSION,
            "config_version": FSRS_CONFIG_VERSION,
            "card": card.to_dict(),
        }
        if migration is not None:
            payload["migration"] = migration
        return payload


class MvpReviewScheduler:
    """Legacy scheduler kept only for explicit compatibility and regression tests."""

    version = "mvp-review-v1"

    def schedule(
        self,
        card: ReviewCard,
        *,
        rating: int,
        reviewed_at: datetime | None = None,
        history: Sequence[HistoricalReview] = (),
    ) -> ScheduleResult:
        if rating not in {1, 2, 3, 4}:
            raise ValueError("rating must be between 1 and 4")
        now = reviewed_at or datetime.now(UTC)
        current = max(0.0, float(card.interval_days or 0.0))
        if rating == 1:
            interval = 10 / (24 * 60)
            lapse_delta = 1
        elif rating == 2:
            interval = 1.0 if current == 0 else max(1.0, current * 1.2)
            lapse_delta = 0
        elif rating == 3:
            interval = 2.0 if current == 0 else max(2.0, current * 2.3)
            lapse_delta = 0
        else:
            interval = 4.0 if current == 0 else max(4.0, current * 3.2)
            lapse_delta = 0
        interval = round(interval, 4)
        previous_state = card.scheduler_state or {}
        difficulty = float(previous_state.get("difficulty", 5.0))
        difficulty = min(10.0, max(1.0, difficulty + {1: 1.0, 2: 0.3, 3: -0.2, 4: -0.6}[rating]))
        return ScheduleResult(
            rating=rating,
            due_at=now + timedelta(days=interval),
            interval_days=interval,
            lapse_delta=lapse_delta,
            scheduler_version=self.version,
            scheduler_state={
                "difficulty": round(difficulty, 2),
                "stability_days": interval,
                "last_rating": rating,
                "algorithm": "transparent_mvp_steps",
            },
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _card_id_as_int(value: Any) -> int:
    if hasattr(value, "int"):
        return int(value.int)
    return int(value)
