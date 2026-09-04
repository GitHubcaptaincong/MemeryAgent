from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import UUID

from memory_agent.config import Settings
from memory_agent.models import utc_now
from memory_agent.reminders import (
    PLAN_NAME,
    THING_MAX_CHARS,
    claim_due_reminders,
    claim_due_card_reminders,
    record_card_reminder_result,
    record_delivery_result,
    truncate_thing,
)
from memory_agent.wechat import WeChatClient, WeChatClientError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DeliveryResult:
    status: str
    errcode: int | None
    errmsg: str | None


def mask_openid(openid: str) -> str:
    if len(openid) <= 8:
        return "***"
    return f"{openid[:4]}...{openid[-4:]}"


def map_template_data(
    logical_data: dict[str, Any], field_map: dict[str, str]
) -> dict[str, dict[str, str]]:
    mapped: dict[str, dict[str, str]] = {}
    for logical_field, template_field in field_map.items():
        if logical_field not in logical_data:
            raise ValueError(f"reminder data is missing field: {logical_field}")
        value = str(logical_data[logical_field])
        if template_field.startswith("thing"):
            value = truncate_thing(value)
        mapped[template_field] = {"value": value}
    return mapped


class ReminderDispatcher:
    """Periodic service-layer dispatcher for due WeChat reminders."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        settings: Settings,
        wechat_client: Any | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self._owns_wechat_client = wechat_client is None
        self.wechat_client = wechat_client or WeChatClient(
            app_id=settings.wechat_app_id or "",
            app_secret=settings.wechat_app_secret_value or "",
            timeout_seconds=settings.wechat_send_timeout_seconds,
            miniprogram_state=settings.wechat_miniprogram_state,
            lang=settings.wechat_subscribe_lang,
        )

    async def dispatch_once(self, *, now: datetime | None = None) -> int:
        tick_started = time.perf_counter()
        logger.debug("reminder_worker.tick.start")
        current = now or utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        logger.debug("reminder worker tick")
        if not self.settings.reminder_delivery_ready:
            logger.debug("reminder delivery is disabled or not fully configured")
            return 0

        try:
            with self.session_factory() as session:
                jobs = claim_due_reminders(
                    session,
                    settings=self.settings,
                    batch_size=self.settings.reminder_batch_size,
                    lease_seconds=self.settings.reminder_lease_seconds,
                    now=current,
                )
        except Exception:
            logger.exception("daily reminder claim failed")
            jobs = []
        logger.info("due reminders claimed", extra={"count": len(jobs)})

        for job in jobs:
            result = await self._send_one(job)
            delivery_id = str(job["id"])
            try:
                with self.session_factory() as session:
                    delivery = record_delivery_result(
                        session,
                        delivery_id=UUID(delivery_id),
                        result_status=result.status,
                        wechat_errcode=result.errcode,
                        wechat_errmsg=result.errmsg,
                    )
                logger.info(
                    "reminder delivery result",
                    extra={
                        "delivery_id": delivery_id,
                        "status": delivery.status,
                        "wechat_errcode": result.errcode,
                    },
                )
            except Exception:
                logger.exception(
                    "reminder delivery result database update failed",
                    extra={"delivery_id": delivery_id, "wechat_errcode": result.errcode},
                )
        try:
            with self.session_factory() as session:
                card_jobs = claim_due_card_reminders(
                    session, settings=self.settings,
                    batch_size=self.settings.reminder_batch_size, now=current,
                )
            logger.info("due card reminders claimed", extra={"count": len(card_jobs)})
            for job in card_jobs:
                result = await self._send_one(job)
                with self.session_factory() as session:
                    record_card_reminder_result(
                        session, delivery_id=UUID(str(job["id"])),
                        result_status=result.status, wechat_errcode=result.errcode,
                        wechat_errmsg=result.errmsg,
                    )
        except Exception:
            logger.exception("card reminder tick failed")
        total = len(jobs) + len(card_jobs)
        logger.debug("reminder_worker.tick.finish", extra={"duration_ms": round((time.perf_counter() - tick_started) * 1000, 1), "daily_claimed_count": len(jobs), "card_claimed_count": len(card_jobs)})
        return total

    async def run_until_stopped(self, stop_event: threading.Event) -> None:
        logger.info(
            "reminder_worker.started",
            extra={
                "interval_seconds": self.settings.reminder_worker_interval_seconds,
                "enabled": self.settings.reminder_delivery_enabled,
            },
        )
        try:
            while not stop_event.is_set():
                try:
                    await self.dispatch_once()
                except Exception:
                    # A database or unexpected worker error must stop this tick,
                    # not permanently stop the worker process.
                    logger.exception("reminder worker tick failed")
                await asyncio.to_thread(
                    stop_event.wait, self.settings.reminder_worker_interval_seconds
                )
        finally:
            if self._owns_wechat_client:
                await self.wechat_client.aclose()

    async def _send_one(self, job: dict[str, Any]) -> _DeliveryResult:
        delivery_id = str(job.get("id", ""))
        openid = str(job.get("openid", ""))
        template_id = str(job.get("template_id", ""))
        try:
            if not delivery_id:
                raise ValueError("claimed reminder is missing id")
            if not openid:
                raise ValueError("claimed reminder is missing openid")
            if not template_id:
                raise ValueError("claimed reminder is missing template_id")
            try:
                data = map_template_data(
                    job.get("data") or {}, self.settings.wechat_template_field_map
                )
            except Exception:
                logger.exception("wechat_reminder.payload_mapping_failed", extra={"card_reminder_id": job.get("card_reminder_id"), "delivery_id": delivery_id, "user_id": job.get("user_id"), "grant_id": job.get("grant_id")})
                raise
            logger.info("wechat_reminder.send.start", extra={"card_reminder_id": job.get("card_reminder_id"), "delivery_id": delivery_id, "user_id": job.get("user_id"), "grant_id": job.get("grant_id"), "template_id": template_id, "openid_present": bool(openid), "payload_keys": list(data), "payload_lengths": {key: len(value["value"]) for key, value in data.items()}})
            logger.info(
                "reminder delivery started",
                extra={
                    "delivery_id": delivery_id,
                    "user_id": str(job.get("user_id", "")),
                    "openid": mask_openid(openid),
                    "template_id": template_id,
                },
            )
            result = await self.wechat_client.send_subscribe_message(
                openid,
                template_id,
                job.get("page"),
                data,
            )
            if result.errcode is None:
                logger.warning("wechat_reminder.send.uncertain", extra={"card_reminder_id": job.get("card_reminder_id"), "delivery_id": delivery_id, "user_id": job.get("user_id"), "template_id": template_id, "result": "uncertain"})
                return _DeliveryResult(
                    status="uncertain",
                    errcode=None,
                    errmsg="WeChat send response did not contain errcode",
                )
            logger.info("wechat_reminder.api.response", extra={"card_reminder_id": job.get("card_reminder_id"), "delivery_id": delivery_id, "user_id": job.get("user_id"), "template_id": template_id, "http_status": 200, "errcode": result.errcode, "errmsg": result.errmsg})
            if result.errcode == 0:
                logger.info("wechat_reminder.send.succeeded", extra={"card_reminder_id": job.get("card_reminder_id"), "delivery_id": delivery_id, "user_id": job.get("user_id")})
            else:
                logger.warning("wechat_reminder.send.failed", extra={"card_reminder_id": job.get("card_reminder_id"), "delivery_id": delivery_id, "user_id": job.get("user_id"), "errcode": result.errcode, "errmsg": result.errmsg, "http_status": 200})
            return _DeliveryResult(
                status="sent" if result.errcode == 0 else "failed",
                errcode=result.errcode,
                errmsg=result.errmsg,
            )
        except WeChatClientError as exc:
            logger.error(
                "WeChat API request failed",
                extra={
                    "delivery_id": delivery_id,
                    "phase": exc.phase,
                    "errcode": exc.errcode,
                    "uncertain": exc.uncertain,
                },
            )
            return _DeliveryResult(
                status="uncertain" if exc.uncertain else "failed",
                errcode=exc.errcode,
                errmsg=exc.errmsg,
            )
        except Exception as exc:
            return _DeliveryResult(
                status="failed",
                errcode=None,
                errmsg=f"{type(exc).__name__}: {exc}",
            )
