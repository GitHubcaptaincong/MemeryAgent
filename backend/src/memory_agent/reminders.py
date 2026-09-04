from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory_agent.config import Settings
from memory_agent.models import (
    ReminderDelivery,
    CardReminder,
    ReviewEvent,
    ReminderPreference,
    ReminderSubscriptionGrant,
    ReviewCard,
    WechatIdentity,
    utc_now,
)


ACCEPTED_RESULTS = {"accept", "acceptWithAudio"}

PLAN_NAME = "今日知识复习"
THING_MAX_CHARS = 20


def build_reminder_logical_data(*, due_count: int, reminder_time: str) -> dict[str, str]:
    """Build the stable logical reminder data consumed by the dispatcher."""
    return {
        "plan_name": PLAN_NAME,
        "due_count": str(due_count),
        "reminder_time": reminder_time,
        "summary": truncate_thing(f"今天有 {due_count} 个知识点建议复习"),
    }


def user_has_reviewed_today(
    session: Session, *, user_id: UUID, timezone_name: str, now: datetime
) -> bool:
    """Return whether a completed rating occurred in the user's local day."""
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return False
    local_now = now.astimezone(timezone)
    local_start = datetime.combine(local_now.date(), time.min, tzinfo=timezone)
    local_next_start = datetime.combine(
        local_now.date() + timedelta(days=1), time.min, tzinfo=timezone
    )
    start_utc = local_start.astimezone(UTC)
    next_start_utc = local_next_start.astimezone(UTC)
    return session.scalar(
        select(ReviewEvent.id).where(
            ReviewEvent.user_id == user_id,
            ReviewEvent.event_type == "review_rated",
            ReviewEvent.created_at >= start_utc,
            ReviewEvent.created_at < next_start_utc,
        ).limit(1)
    ) is not None


def record_subscription_result(
    session: Session,
    *,
    user_id: UUID,
    template_id: str,
    result: str,
    idempotency_key: str,
) -> ReminderSubscriptionGrant:
    existing = session.scalar(
        select(ReminderSubscriptionGrant).where(
            ReminderSubscriptionGrant.user_id == user_id,
            ReminderSubscriptionGrant.idempotency_key == idempotency_key,
        )
    )
    desired_status = "available" if result in ACCEPTED_RESULTS else result
    if existing is not None:
        if existing.template_id != template_id or existing.status != desired_status:
            raise ValueError("idempotency key was used for another subscription result")
        return existing
    grant = ReminderSubscriptionGrant(
        user_id=user_id,
        template_id=template_id,
        idempotency_key=idempotency_key,
        status=desired_status,
    )
    session.add(grant)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(ReminderSubscriptionGrant).where(
                ReminderSubscriptionGrant.user_id == user_id,
                ReminderSubscriptionGrant.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise
        return existing
    return grant


def reminder_status(
    session: Session, *, user_id: UUID, settings: Settings
) -> dict[str, Any]:
    template_id = settings.wechat_subscribe_template_id
    grant_count = 0
    if template_id:
        grant_count = int(
            session.scalar(
                select(func.count(ReminderSubscriptionGrant.id)).where(
                    ReminderSubscriptionGrant.user_id == user_id,
                    ReminderSubscriptionGrant.template_id == template_id,
                    ReminderSubscriptionGrant.status == "available",
                )
            )
            or 0
        )
    latest = session.scalar(
        select(ReminderDelivery)
        .where(ReminderDelivery.user_id == user_id)
        .order_by(ReminderDelivery.created_at.desc())
        .limit(1)
    )
    return {
        "template_id": template_id,
        "delivery_enabled": bool(
            settings.reminder_delivery_enabled
            and template_id
            and settings.wechat_app_id
            and settings.wechat_app_secret_value
        ),
        "available_grants": grant_count,
        "last_delivery_status": latest.status if latest else None,
        "last_sent_at": latest.sent_at if latest else None,
    }


def claim_due_reminders(
    session: Session,
    *,
    settings: Settings,
    batch_size: int,
    lease_seconds: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if not settings.reminder_delivery_enabled or not settings.wechat_subscribe_template_id:
        return []
    current = now or datetime.now(UTC)
    preferences = session.scalars(
        select(ReminderPreference)
        .where(ReminderPreference.enabled.is_(True))
        .with_for_update(skip_locked=True)
    ).all()
    jobs: list[dict[str, Any]] = []
    for preference in preferences:
        if len(jobs) >= batch_size:
            break
        try:
            local_now = current.astimezone(ZoneInfo(preference.timezone))
        except ZoneInfoNotFoundError:
            continue
        if local_now.strftime("%H:%M") < preference.preferred_time:
            continue
        if user_has_reviewed_today(
            session,
            user_id=preference.user_id,
            timezone_name=preference.timezone,
            now=current,
        ):
            continue
        local_date = local_now.date().isoformat()
        existing = session.scalar(
            select(ReminderDelivery).where(
                ReminderDelivery.user_id == preference.user_id,
                ReminderDelivery.template_id == settings.wechat_subscribe_template_id,
                ReminderDelivery.local_date == local_date,
            )
        )
        if existing is not None:
            if existing.status == "sending" and _as_utc(
                existing.last_attempt_at
            ) <= current - timedelta(seconds=lease_seconds):
                # The dispatcher may have sent successfully and then failed to
                # report the result. Prefer a missed reminder over a duplicate.
                existing.status = "uncertain"
                existing.error_code = "dispatch_result_missing"
                existing.last_attempt_at = current
            if existing.status != "failed":
                continue
            retry_delays = (60, 300, 1800)
            if existing.attempt_count >= len(retry_delays):
                continue
            if existing.last_attempt_at and _as_utc(existing.last_attempt_at) > current - timedelta(seconds=retry_delays[existing.attempt_count - 1]):
                continue
            grant = session.scalar(
                select(ReminderSubscriptionGrant).where(
                    ReminderSubscriptionGrant.user_id == preference.user_id,
                    ReminderSubscriptionGrant.template_id == settings.wechat_subscribe_template_id,
                    ReminderSubscriptionGrant.status == "available",
                ).order_by(ReminderSubscriptionGrant.created_at).limit(1).with_for_update(skip_locked=True)
            )
            if grant is None:
                continue
            grant.status = "reserved"
            delivery = existing
            delivery.grant_id = grant.id
            delivery.status = "sending"
            delivery.attempt_count += 1
            delivery.last_attempt_at = current
        else:
            due_filters = [
                ReviewCard.user_id == preference.user_id,
                ReviewCard.status == "active",
                ReviewCard.due_at <= current,
            ]
            if not preference.overdue_enabled:
                local_midnight = local_now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).astimezone(UTC)
                due_filters.append(ReviewCard.due_at >= local_midnight)
            due_count = int(
                session.scalar(
                    select(func.count(ReviewCard.id)).where(*due_filters)
                )
                or 0
            )
            if due_count <= 0:
                continue
            grant = session.scalar(
                select(ReminderSubscriptionGrant)
                .where(
                    ReminderSubscriptionGrant.user_id == preference.user_id,
                    ReminderSubscriptionGrant.template_id
                    == settings.wechat_subscribe_template_id,
                    ReminderSubscriptionGrant.status == "available",
                )
                .order_by(ReminderSubscriptionGrant.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if grant is None:
                continue
            grant.status = "reserved"
            delivery = ReminderDelivery(
                user_id=preference.user_id,
                grant_id=grant.id,
                template_id=settings.wechat_subscribe_template_id,
                local_date=local_date,
                scheduled_for=current,
                due_count=min(due_count, preference.daily_limit),
                status="sending",
                reminder_type="daily",
                attempt_count=1,
                last_attempt_at=current,
            )
            session.add(delivery)
            session.flush()
        identity = session.scalar(
            select(WechatIdentity).where(WechatIdentity.user_id == preference.user_id)
        )
        if identity is None:
            delivery.status = "failed"
            delivery.error_code = "wechat_identity_missing"
            if delivery.grant_id:
                reserved_grant = session.get(
                    ReminderSubscriptionGrant, delivery.grant_id
                )
                if reserved_grant is not None:
                    reserved_grant.status = "available"
            continue
        jobs.append(
            {
                "id": str(delivery.id),
                "user_id": str(delivery.user_id),
                "openid": identity.openid,
                "template_id": delivery.template_id,
                "page": settings.wechat_subscribe_page,
                "data": build_reminder_logical_data(
                    due_count=delivery.due_count,
                    reminder_time=local_now.strftime("%Y-%m-%d %H:%M"),
                ),
            }
        )
    session.commit()
    return jobs


def claim_due_card_reminders(
    session: Session, *, settings: Settings, batch_size: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if not settings.reminder_delivery_ready:
        return []
    current = now or datetime.now(UTC)
    rows = session.scalars(
        select(CardReminder).where(
            CardReminder.status == "scheduled", CardReminder.scheduled_for <= current
        ).with_for_update(skip_locked=True).limit(batch_size)
    ).all()
    jobs: list[dict[str, Any]] = []
    for reminder in rows:
        card = session.get(ReviewCard, reminder.review_card_id)
        identity = session.scalar(select(WechatIdentity).where(WechatIdentity.user_id == reminder.user_id))
        grant = session.scalar(select(ReminderSubscriptionGrant).where(
            ReminderSubscriptionGrant.user_id == reminder.user_id,
            ReminderSubscriptionGrant.template_id == settings.wechat_subscribe_template_id,
            ReminderSubscriptionGrant.status == "available",
        ).order_by(ReminderSubscriptionGrant.created_at).limit(1).with_for_update(skip_locked=True))
        if card is None or card.user_id != reminder.user_id or identity is None or grant is None:
            continue
        reminder.status = "sending"
        reminder.attempt_count += 1
        reminder.last_attempt_at = current
        grant.status = "reserved"
        delivery = ReminderDelivery(
            user_id=reminder.user_id, grant_id=grant.id,
            template_id=settings.wechat_subscribe_template_id,
            local_date=None, reminder_type="card", card_reminder_id=reminder.id,
            scheduled_for=reminder.scheduled_for, due_count=1,
            status="sending", attempt_count=reminder.attempt_count,
            last_attempt_at=current,
        )
        session.add(delivery)
        session.flush()
        jobs.append({
            "id": str(delivery.id), "user_id": str(reminder.user_id),
            "openid": identity.openid, "template_id": delivery.template_id,
            "page": settings.wechat_subscribe_page,
            "data": {
                "plan_name": "知识点复习",
                "reminder_time": reminder.scheduled_for.strftime("%Y-%m-%d %H:%M"),
                "summary": truncate_thing(f"复习：{card.draft_unit.title}"),
            },
            "card_reminder_id": str(reminder.id),
        })
    session.commit()
    return jobs


def record_card_reminder_result(session: Session, *, delivery_id: UUID, result_status: str,
                                wechat_errcode: int | None, wechat_errmsg: str | None) -> ReminderDelivery:
    delivery = session.scalar(select(ReminderDelivery).where(ReminderDelivery.id == delivery_id).with_for_update())
    if delivery is None:
        raise LookupError("reminder delivery not found")
    delivery.status = result_status
    delivery.response_code = wechat_errcode
    delivery.error_code = wechat_errmsg[:100] if wechat_errmsg else None
    delivery.last_attempt_at = utc_now()
    reminder = session.get(CardReminder, delivery.card_reminder_id)
    grant = session.get(ReminderSubscriptionGrant, delivery.grant_id) if delivery.grant_id else None
    if reminder is not None:
        reminder.status = result_status
    if result_status == "sent":
        delivery.sent_at = utc_now()
        if grant is not None:
            grant.status = "consumed"; grant.consumed_at = delivery.sent_at
    elif result_status == "failed" and grant is not None:
        grant.status = "available"
    session.commit()
    return delivery


def record_delivery_result(
    session: Session,
    *,
    delivery_id: UUID,
    result_status: str,
    wechat_errcode: int | None,
    wechat_errmsg: str | None,
) -> ReminderDelivery:
    delivery = session.scalar(
        select(ReminderDelivery)
        .where(ReminderDelivery.id == delivery_id)
        .with_for_update()
    )
    if delivery is None:
        raise LookupError("reminder delivery not found")
    if delivery.status == "sent":
        return delivery
    delivery.status = result_status
    delivery.response_code = wechat_errcode
    delivery.error_code = wechat_errmsg[:100] if wechat_errmsg else None
    delivery.last_attempt_at = utc_now()
    grant = session.get(ReminderSubscriptionGrant, delivery.grant_id) if delivery.grant_id else None
    if result_status == "sent":
        delivery.sent_at = utc_now()
        if grant is not None:
            grant.status = "consumed"
            grant.consumed_at = delivery.sent_at
    elif result_status == "failed" and grant is not None:
        grant.status = "invalid" if wechat_errcode in {40037, 43101} else "available"
    # An uncertain network result keeps the grant reserved and is never auto-retried.
    session.commit()
    return delivery


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def truncate_thing(value: str, *, max_chars: int = THING_MAX_CHARS) -> str:
    """WeChat ``thing`` type fields accept at most 20 Chinese characters."""
    return value if len(value) <= max_chars else value[: max_chars - 1] + "…"
