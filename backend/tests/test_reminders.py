from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from memory_agent.config import Settings
from memory_agent.database import Base, SessionLocal, engine
from memory_agent.models import (
    AgentProfile,
    AgentRun,
    DraftUnit,
    KnowledgeDraft,
    ReminderDelivery,
    ReminderPreference,
    ReminderSubscriptionGrant,
    ReviewCard,
    Source,
    User,
    WechatIdentity,
)
from memory_agent.reminders import (
    claim_due_reminders,
    record_delivery_result,
    record_subscription_result,
)


TEMPLATE_ID = "reminder-template-for-tests"
FIXED_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def ensure_reminder_schema() -> None:
    Base.metadata.create_all(engine)


def _settings() -> Settings:
    return Settings(
        model_provider="fake",
        reminder_delivery_enabled=True,
        wechat_subscribe_template_id=TEMPLATE_ID,
        wechat_subscribe_page="pages/review/review",
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _seed_user(
    session,
    *,
    label: str,
    due_cards: int = 1,
    daily_limit: int = 10,
    with_identity: bool = True,
) -> tuple[UUID, str]:
    user_id = uuid4()
    suffix = uuid4().hex
    user = User(id=user_id, email=f"{label}-{suffix}@example.invalid", timezone="UTC")
    session.add(user)
    session.flush()

    profile = AgentProfile(
        user_id=user_id,
        name="default",
        core_profile_summary="",
        config_json={},
    )
    source = Source(
        user_id=user_id,
        title=f"{label} source",
        learning_goal="Test reminder isolation",
        raw_content="Reminder test source",
        content_type="text",
        content_hash=suffix.ljust(64, "0")[:64],
        char_count=20,
        web_access_allowed=False,
        status="active",
    )
    session.add_all([profile, source])
    session.flush()

    run = AgentRun(
        user_id=user_id,
        profile_id=profile.id,
        source_id=source.id,
        state="completed",
        idempotency_key=f"reminder-test-run-{suffix}",
        model_provider="fake",
    )
    session.add(run)
    session.flush()
    draft = KnowledgeDraft(
        user_id=user_id,
        source_id=source.id,
        run_id=run.id,
        status="confirmed",
        learning_goal="Test reminders",
        agent_summary={},
        confirmed_at=FIXED_NOW - timedelta(days=1),
    )
    session.add(draft)
    session.flush()

    for position in range(due_cards):
        unit = DraftUnit(
            draft_id=draft.id,
            position=position,
            title=f"{label} unit {position}",
            learning_objective="Recall the test fact",
            explanation="A deterministic reminder test unit.",
            question=f"Question {position}?",
            answer_key=["Answer"],
        )
        session.add(unit)
        session.flush()
        session.add(
            ReviewCard(
                user_id=user_id,
                draft_unit_id=unit.id,
                status="active",
                due_at=FIXED_NOW - timedelta(hours=1),
                interval_days=0.0,
                scheduler_state={},
            )
        )

    session.add(
        ReminderPreference(
            user_id=user_id,
            enabled=True,
            preferred_time="00:00",
            daily_limit=daily_limit,
            overdue_enabled=True,
            timezone="UTC",
        )
    )
    openid = f"openid-{label}-{suffix}"
    if with_identity:
        session.add(
            WechatIdentity(
                user_id=user_id,
                app_id="wx-reminder-tests",
                openid=openid,
            )
        )
    session.commit()
    return user_id, openid


def _record_grant(session, user_id: UUID, key: str) -> ReminderSubscriptionGrant:
    return record_subscription_result(
        session,
        user_id=user_id,
        template_id=TEMPLATE_ID,
        result="accept",
        idempotency_key=key,
    )


def test_one_time_subscription_grant_is_idempotent_and_user_scoped() -> None:
    with SessionLocal() as session:
        first_user, _ = _seed_user(session, label="grant-first", due_cards=0)
        second_user, _ = _seed_user(session, label="grant-second", due_cards=0)

        first = _record_grant(session, first_user, "shared-subscription-key")
        duplicate = record_subscription_result(
            session,
            user_id=first_user,
            template_id=TEMPLATE_ID,
            result="acceptWithAudio",
            idempotency_key="shared-subscription-key",
        )
        other_user = _record_grant(session, second_user, "shared-subscription-key")

        assert duplicate.id == first.id
        assert first.status == "available"
        assert other_user.id != first.id
        assert (
            session.scalar(
                select(func.count(ReminderSubscriptionGrant.id)).where(
                    ReminderSubscriptionGrant.user_id == first_user
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(ReminderSubscriptionGrant.id)).where(
                    ReminderSubscriptionGrant.user_id == second_user
                )
            )
            == 1
        )

        with pytest.raises(
            ValueError,
            match="idempotency key was used for another subscription result",
        ):
            record_subscription_result(
                session,
                user_id=first_user,
                template_id="another-template",
                result="accept",
                idempotency_key="shared-subscription-key",
            )


def test_claim_reserves_grant_and_applies_daily_limit() -> None:
    with SessionLocal() as session:
        user_id, openid = _seed_user(
            session,
            label="claim",
            due_cards=3,
            daily_limit=2,
        )
        grant = _record_grant(session, user_id, "claim-grant-key")

        jobs = claim_due_reminders(
            session,
            settings=_settings(),
            batch_size=10,
            lease_seconds=120,
            now=FIXED_NOW,
        )

        assert len(jobs) == 1
        assert jobs[0]["openid"] == openid
        assert jobs[0]["template_id"] == TEMPLATE_ID
        assert jobs[0]["data"]["due_count"] == "2"

        session.refresh(grant)
        delivery = session.scalar(
            select(ReminderDelivery).where(ReminderDelivery.user_id == user_id)
        )
        assert grant.status == "reserved"
        assert delivery is not None
        assert delivery.grant_id == grant.id
        assert delivery.status == "sending"
        assert delivery.due_count == 2


def test_sent_delivery_consumes_grant_and_is_not_claimed_twice_same_day() -> None:
    with SessionLocal() as session:
        user_id, _ = _seed_user(session, label="sent", due_cards=1)
        grant = _record_grant(session, user_id, "sent-grant-key")
        jobs = claim_due_reminders(
            session,
            settings=_settings(),
            batch_size=10,
            lease_seconds=120,
            now=FIXED_NOW,
        )
        assert len(jobs) == 1

        delivery_id = UUID(jobs[0]["id"])
        delivery = record_delivery_result(
            session,
            delivery_id=delivery_id,
            result_status="sent",
            wechat_errcode=0,
            wechat_errmsg="ok",
        )
        session.refresh(delivery)
        session.refresh(grant)
        assert delivery.status == "sent"
        assert delivery.sent_at is not None
        assert grant.status == "consumed"
        assert _utc(grant.consumed_at) == _utc(delivery.sent_at)

        repeated = claim_due_reminders(
            session,
            settings=_settings(),
            batch_size=10,
            lease_seconds=120,
            now=FIXED_NOW + timedelta(hours=1),
        )
        assert repeated == []
        assert (
            session.scalar(
                select(func.count(ReminderDelivery.id)).where(
                    ReminderDelivery.user_id == user_id
                )
            )
            == 1
        )


def test_expired_sending_lease_becomes_uncertain_and_is_never_resent() -> None:
    with SessionLocal() as session:
        user_id, _ = _seed_user(session, label="expired", due_cards=1)
        grant = _record_grant(session, user_id, "expired-grant-key")
        jobs = claim_due_reminders(
            session,
            settings=_settings(),
            batch_size=10,
            lease_seconds=120,
            now=FIXED_NOW,
        )
        assert len(jobs) == 1
        delivery_id = UUID(jobs[0]["id"])

        after_expiry = claim_due_reminders(
            session,
            settings=_settings(),
            batch_size=10,
            lease_seconds=120,
            now=FIXED_NOW + timedelta(seconds=121),
        )
        assert after_expiry == []

        delivery = session.get(ReminderDelivery, delivery_id)
        session.refresh(grant)
        assert delivery is not None
        assert delivery.status == "uncertain"
        assert delivery.error_code == "dispatch_result_missing"
        assert delivery.attempt_count == 1
        assert grant.status == "reserved"

        much_later = claim_due_reminders(
            session,
            settings=_settings(),
            batch_size=10,
            lease_seconds=120,
            now=FIXED_NOW + timedelta(hours=2),
        )
        assert much_later == []
        assert (
            session.scalar(
                select(func.count(ReminderDelivery.id)).where(
                    ReminderDelivery.user_id == user_id
                )
            )
            == 1
        )


def test_claim_never_borrows_another_users_grant_or_identity() -> None:
    with SessionLocal() as session:
        user_without_grant, first_openid = _seed_user(
            session,
            label="isolated-no-grant",
            due_cards=4,
        )
        eligible_user, eligible_openid = _seed_user(
            session,
            label="isolated-eligible",
            due_cards=1,
        )
        eligible_grant = _record_grant(
            session,
            eligible_user,
            "isolated-eligible-grant",
        )

        jobs = claim_due_reminders(
            session,
            settings=_settings(),
            batch_size=10,
            lease_seconds=120,
            now=FIXED_NOW,
        )

        assert len(jobs) == 1
        assert jobs[0]["openid"] == eligible_openid
        assert jobs[0]["openid"] != first_openid
        assert jobs[0]["data"]["due_count"] == "1"
        session.refresh(eligible_grant)
        assert eligible_grant.status == "reserved"
        assert (
            session.scalar(
                select(func.count(ReminderDelivery.id)).where(
                    ReminderDelivery.user_id == user_without_grant
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(ReminderDelivery.id)).where(
                    ReminderDelivery.user_id == eligible_user
                )
            )
            == 1
        )
