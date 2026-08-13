from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory_agent.config import Settings
from memory_agent.models import (
    ReminderDelivery,
    ReminderPreference,
    ReminderSubscriptionGrant,
    ReviewCard,
    WechatIdentity,
    utc_now,
)


ACCEPTED_RESULTS = {"accept", "acceptWithAudio"}


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
            and settings.reminder_dispatch_token_value
        ),
        "available_grants": grant_count,
        "last_delivery_status": latest.status if latest else None,
        "last_sent_at": latest.sent_at if latest else None,
    }


def verify_dispatch_token(provided: str | None, settings: Settings) -> None:
    expected = settings.reminder_dispatch_token_value
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid reminder dispatch token",
        )


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
            continue
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
                "openid": identity.openid,
                "template_id": delivery.template_id,
                "page": settings.wechat_subscribe_page,
                "data": {
                    "due_count": str(delivery.due_count),
                    "reminder_time": local_now.strftime("%Y-%m-%d %H:%M"),
                    "summary": f"今天有 {delivery.due_count} 个知识点建议复习",
                },
            }
        )
    session.commit()
    return jobs


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
