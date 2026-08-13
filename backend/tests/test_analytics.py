from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from memory_agent.analytics import get_daily_plan, get_review_insights
from memory_agent.database import Base, SessionLocal, engine
from memory_agent.models import (
    AgentProfile,
    AgentRun,
    DraftUnit,
    KnowledgeDraft,
    ReviewCard,
    ReviewEvent,
    Source,
    User,
)


def test_review_insights_and_daily_plan_are_isolated_and_read_only() -> None:
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 13, 2, 0, tzinfo=UTC)
    session = SessionLocal()
    transaction = session.begin()
    try:
        user_id, cards = _seed_review_data(session, now=now, prefix="analytics-main")
        other_user_id, other_cards = _seed_review_data(
            session, now=now, prefix="analytics-other", only_weak=True
        )
        before_cards = _card_snapshot(session, [*cards.values(), *other_cards.values()])
        before_events = _event_snapshot(session, user_ids=[user_id, other_user_id])

        insights = get_review_insights(
            session,
            user_id=user_id,
            trend_days=7,
            forecast_days=5,
            weak_limit=10,
            timezone="Asia/Shanghai",
            daily_limit=2,
            now=now,
        )
        plan = get_daily_plan(
            session,
            user_id=user_id,
            daily_limit=2,
            timezone="Asia/Shanghai",
            overdue_enabled=True,
            include_overflow=False,
            now=now,
        )

        assert insights["summary"]["active_card_count"] == 3
        assert insights["summary"]["reviewed_card_count"] == 2
        assert insights["summary"]["unreviewed_count"] == 1
        weak_ids = [item["card_id"] for item in insights["weak_cards"]]
        assert cards["weak"] in weak_ids
        assert cards["steady"] in weak_ids
        assert cards["unreviewed"] not in weak_ids
        assert other_cards["weak"] not in weak_ids
        assert insights["weak_cards"][0]["card_id"] == cards["weak"]
        assert insights["weak_cards"][0]["weakness_score"] > insights["weak_cards"][1]["weakness_score"]
        assert insights["weak_cards"][0]["reasons"]

        daily = insights["trend"]["daily"]
        assert len(daily) == 7
        assert [item["date"] for item in daily] == [
            "2026-08-07",
            "2026-08-08",
            "2026-08-09",
            "2026-08-10",
            "2026-08-11",
            "2026-08-12",
            "2026-08-13",
        ]
        assert daily[1]["completed_count"] == 0
        assert insights["trend"]["summary"]["completed_count"] == 5
        assert insights["trend"]["summary"]["unique_cards"] == 2
        assert insights["trend"]["summary"]["self_rated_mastery_rate"] == 0.6

        assert plan["completed_today"] == 1
        assert plan["remaining_capacity"] == 1
        assert plan["due_now_count"] == 2
        assert plan["overdue_backlog_count"] == 1
        assert plan["must_do_count"] == 1
        assert plan["planned_count"] == 1
        assert plan["planned_cards"][0]["id"] == cards["unreviewed"]
        assert plan["overflow_count"] == 1
        assert plan["overflow_cards"] == []
        assert plan["fsrs_schedule_changed"] is False

        must_do_overrun = get_daily_plan(
            session,
            user_id=user_id,
            daily_limit=1,
            timezone="Asia/Shanghai",
            overdue_enabled=True,
            now=now,
        )
        assert must_do_overrun["remaining_capacity"] == 0
        assert must_do_overrun["planned_cards"][0]["id"] == cards["unreviewed"]
        assert must_do_overrun["soft_limit_exceeded"] is True

        full_plan = get_daily_plan(
            session,
            user_id=user_id,
            daily_limit=2,
            timezone="Asia/Shanghai",
            overdue_enabled=True,
            include_overflow=True,
            now=now,
        )
        assert [item["id"] for item in full_plan["overflow_cards"]] == [cards["weak"]]
        assert insights["workload"]["daily"][0]["recommended_count"] == 1
        assert len(insights["workload"]["daily"]) == 5

        session.expire_all()
        assert _card_snapshot(session, [*cards.values(), *other_cards.values()]) == before_cards
        assert _event_snapshot(session, user_ids=[user_id, other_user_id]) == before_events
        assert not session.new
        assert not session.dirty
        assert not session.deleted
    finally:
        transaction.rollback()
        session.close()


def test_empty_insights_zero_fill_trend_and_forecast() -> None:
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 13, 2, 0, tzinfo=UTC)
    session = SessionLocal()
    transaction = session.begin()
    try:
        user = User(id=uuid.uuid4(), email=f"empty-{uuid.uuid4()}@example.test")
        session.add(user)
        session.flush()

        insights = get_review_insights(
            session,
            user_id=user.id,
            trend_days=3,
            forecast_days=4,
            weak_limit=5,
            daily_limit=10,
            now=now,
        )
        plan = get_daily_plan(session, user_id=user.id, daily_limit=10, now=now)

        assert insights["summary"] == {
            "active_card_count": 0,
            "due_count": 0,
            "reviewed_card_count": 0,
            "unreviewed_count": 0,
            "completed_in_period": 0,
            "self_rated_mastery_rate": 0.0,
        }
        assert insights["weak_cards"] == []
        assert insights["weak_tags"] == []
        assert len(insights["trend"]["daily"]) == 3
        assert all(item["completed_count"] == 0 for item in insights["trend"]["daily"])
        assert len(insights["workload"]["daily"]) == 4
        assert all(
            item["canonical_due_count"] == 0 and item["recommended_count"] == 0
            for item in insights["workload"]["daily"]
        )
        assert plan["balance_status"] == "clear"
        assert plan["planned_cards"] == []
        assert plan["overflow_count"] == 0
    finally:
        transaction.rollback()
        session.close()


def _seed_review_data(
    session, *, now: datetime, prefix: str, only_weak: bool = False
) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    user_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    source_id = uuid.uuid4()
    run_id = uuid.uuid4()
    draft_id = uuid.uuid4()
    session.add(
        User(
            id=user_id,
            email=f"{prefix}-{uuid.uuid4()}@example.test",
            timezone="Asia/Shanghai",
        )
    )
    session.flush()
    session.add(
        AgentProfile(
            id=profile_id,
            user_id=user_id,
            name="default",
            core_profile_summary="",
            config_json={},
        )
    )
    session.add(
        Source(
            id=source_id,
            user_id=user_id,
            title=f"{prefix} source",
            learning_goal="test analytics",
            raw_content="analytics source",
            content_hash=(prefix.encode().hex() + "0" * 64)[:64],
            char_count=16,
        )
    )
    session.flush()
    session.add(
        AgentRun(
            id=run_id,
            user_id=user_id,
            profile_id=profile_id,
            source_id=source_id,
            state="completed",
            idempotency_key=f"{prefix}-run-{uuid.uuid4()}",
            model_provider="fake",
            config_json={},
        )
    )
    session.flush()
    session.add(
        KnowledgeDraft(
            id=draft_id,
            user_id=user_id,
            source_id=source_id,
            run_id=run_id,
            status="confirmed",
            learning_goal="test analytics",
            agent_summary={},
            confirmed_at=now,
        )
    )
    session.flush()

    definitions = [
        {
            "name": "weak",
            "title": "Weak concept",
            "due_at": now - timedelta(days=2),
            "state": 2,
            "difficulty": 8.2,
            "review_count": 3,
            "lapse_count": 1,
            "ratings": [(3, 3), (2, 1), (1, 0)],
        }
    ]
    if not only_weak:
        definitions.extend(
            [
                {
                    "name": "steady",
                    "title": "Steady concept",
                    "due_at": now + timedelta(days=1),
                    "state": 2,
                    "difficulty": 3.0,
                    "review_count": 2,
                    "lapse_count": 0,
                    "ratings": [(3, 4), (4, 2)],
                },
                {
                    "name": "unreviewed",
                    "title": "New concept",
                    "due_at": now - timedelta(minutes=30),
                    "state": 1,
                    "difficulty": None,
                    "review_count": 0,
                    "lapse_count": 0,
                    "ratings": [],
                },
            ]
        )

    card_ids: dict[str, uuid.UUID] = {}
    for position, definition in enumerate(definitions, start=1):
        unit_id = uuid.uuid4()
        card_id = uuid.uuid4()
        card_ids[definition["name"]] = card_id
        session.add(
            DraftUnit(
                id=unit_id,
                draft_id=draft_id,
                position=position,
                title=definition["title"],
                learning_objective="Understand the concept",
                explanation="Explanation",
                key_points=["point"],
                question=f"Explain {definition['title']}",
                answer_key=["answer"],
                hints=[],
                tags=["shared", definition["name"]],
                applicable_scenarios=[],
                confidence=0.9,
            )
        )
        scheduler_card = {
            "state": definition["state"],
            "difficulty": definition["difficulty"],
            "stability": 4.0,
            "due": definition["due_at"].isoformat(),
        }
        session.add(
            ReviewCard(
                id=card_id,
                user_id=user_id,
                draft_unit_id=unit_id,
                status="active",
                due_at=definition["due_at"],
                interval_days=2.0,
                review_count=definition["review_count"],
                lapse_count=definition["lapse_count"],
                scheduler_version="fsrs-test-v1",
                scheduler_state={"algorithm": "fsrs-6", "card": scheduler_card},
            )
        )
        session.flush()
        for rating, days_ago in definition["ratings"]:
            reviewed_at = now - timedelta(days=days_ago)
            session.add(
                ReviewEvent(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    card_id=card_id,
                    correlation_id=uuid.uuid4(),
                    event_type="review_rated",
                    idempotency_key=f"{prefix}-{definition['name']}-{rating}-{days_ago}-{uuid.uuid4()}",
                    payload_json={
                        "rating": rating,
                        "reviewed_at": reviewed_at.isoformat(),
                        "scheduler_card_before": {"state": 2},
                    },
                    created_at=reviewed_at,
                )
            )
    session.flush()
    return user_id, card_ids


def _card_snapshot(session, card_ids: list[uuid.UUID]) -> list[tuple[Any, ...]]:
    cards = session.scalars(
        select(ReviewCard).where(ReviewCard.id.in_(card_ids)).order_by(ReviewCard.id)
    ).all()
    return [
        (
            card.id,
            card.due_at.isoformat(),
            card.interval_days,
            card.review_count,
            card.lapse_count,
            card.scheduler_version,
            json.dumps(card.scheduler_state, sort_keys=True, default=str),
        )
        for card in cards
    ]


def _event_snapshot(session, *, user_ids: list[uuid.UUID]) -> list[tuple[Any, ...]]:
    events = session.scalars(
        select(ReviewEvent)
        .where(ReviewEvent.user_id.in_(user_ids))
        .order_by(ReviewEvent.id)
    ).all()
    return [
        (
            event.id,
            event.event_type,
            event.created_at.isoformat(),
            json.dumps(event.payload_json, sort_keys=True, default=str),
        )
        for event in events
    ]
