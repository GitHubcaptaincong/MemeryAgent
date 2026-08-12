from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from memory_agent.models import ReviewCard
from memory_agent.scheduler import FsrsReviewScheduler, HistoricalReview


def _card(*, now: datetime, scheduler: FsrsReviewScheduler) -> ReviewCard:
    card_id = uuid.uuid4()
    return ReviewCard(
        id=card_id,
        user_id=uuid.uuid4(),
        draft_unit_id=uuid.uuid4(),
        status="active",
        due_at=now,
        interval_days=0,
        review_count=0,
        lapse_count=0,
        scheduler_version=scheduler.version,
        scheduler_state=scheduler.initial_state(card_id=card_id, due_at=now),
    )


def test_fsrs_state_round_trip_and_learning_transition() -> None:
    scheduler = FsrsReviewScheduler()
    now = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)
    card = _card(now=now, scheduler=scheduler)

    first = scheduler.schedule(card, rating=3, reviewed_at=now)
    assert first.due_at == now + timedelta(minutes=10)
    assert first.scheduler_state["config_version"] == "fsrs-default-no-fuzz-v1"
    assert first.scheduler_state["card"]["state"] == 1
    assert first.review_log is not None

    card.scheduler_state = first.scheduler_state
    card.scheduler_version = first.scheduler_version
    card.due_at = first.due_at
    card.interval_days = first.interval_days
    card.review_count = 1
    second = scheduler.schedule(card, rating=3, reviewed_at=first.due_at)
    assert second.due_at > first.due_at
    assert second.scheduler_state["card"]["state"] == 2

    card.scheduler_state = second.scheduler_state
    card.due_at = second.due_at
    card.review_count = 2
    lapse = scheduler.schedule(card, rating=1, reviewed_at=second.due_at)
    assert lapse.lapse_delta == 1
    assert lapse.scheduler_state["card"]["state"] == 3


def test_legacy_card_is_replayed_from_immutable_history() -> None:
    scheduler = FsrsReviewScheduler()
    first_review = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)
    legacy = ReviewCard(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        draft_unit_id=uuid.uuid4(),
        status="active",
        due_at=first_review + timedelta(days=2),
        interval_days=2,
        review_count=1,
        lapse_count=0,
        scheduler_version="mvp-review-v1",
        scheduler_state={"algorithm": "transparent_mvp_steps"},
    )
    history = [HistoricalReview(rating=3, reviewed_at=first_review)]

    migrated = scheduler.schedule(
        legacy,
        rating=3,
        reviewed_at=first_review + timedelta(days=2),
        history=history,
    )
    assert migrated.scheduler_state["migration"] == {
        "from_scheduler_version": "mvp-review-v1",
        "from_config_version": None,
        "replayed_review_count": 1,
    }
    assert migrated.scheduler_card_before is not None
    assert migrated.scheduler_card_after is not None


def test_legacy_card_with_incomplete_history_is_not_silently_reset() -> None:
    scheduler = FsrsReviewScheduler()
    now = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)
    legacy = ReviewCard(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        draft_unit_id=uuid.uuid4(),
        status="active",
        due_at=now,
        interval_days=2,
        review_count=2,
        lapse_count=0,
        scheduler_version="mvp-review-v1",
        scheduler_state={"algorithm": "transparent_mvp_steps"},
    )

    with pytest.raises(ValueError, match="history is incomplete"):
        scheduler.schedule(legacy, rating=3, reviewed_at=now, history=[])
