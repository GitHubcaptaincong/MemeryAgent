from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import timedelta
import time

import httpx2
import pytest
from sqlalchemy import select

from memory_agent.config import Settings
from memory_agent.database import Base, SessionLocal, engine
from memory_agent.models import (
    ReminderDelivery,
    ReminderPreference,
    ReminderSubscriptionGrant,
)
from memory_agent.reminder_worker import ReminderDispatcher
from memory_agent.wechat import WeChatClient, WeChatClientError, WeChatSendResult
from test_reminders import (
    FIXED_NOW,
    TEMPLATE_ID,
    _record_grant,
    _seed_user,
)


@pytest.fixture(scope="module", autouse=True)
def ensure_worker_schema() -> None:
    Base.metadata.create_all(engine)


def _worker_settings(**overrides) -> Settings:
    values = {
        "model_provider": "fake",
        "reminder_delivery_enabled": True,
        "wechat_app_id": "wx-worker-tests",
        "wechat_app_secret": "worker-test-secret",
        "wechat_subscribe_template_id": TEMPLATE_ID,
        "wechat_subscribe_page": "pages/review/review",
        "reminder_batch_size": 20,
        "reminder_lease_seconds": 120,
    }
    values.update(overrides)
    return Settings(**values)


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class FakeHTTPClient:
    def __init__(self, responses: list[dict | BaseException]):
        self.responses = responses
        self.requests: list[tuple[str, str, dict, dict | None]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict,
        json: dict | None = None,
    ):
        self.requests.append((method, path, params, json))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return FakeResponse(response)

    async def aclose(self) -> None:
        return None


@dataclass
class FakeWeChatClient:
    results: list[WeChatSendResult | BaseException]
    calls: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    async def send_subscribe_message(
        self,
        openid: str,
        template_id: str,
        page: str | None,
        data: dict,
    ) -> WeChatSendResult:
        self.calls.append(
            {
                "openid": openid,
                "template_id": template_id,
                "page": page,
                "data": data,
            }
        )
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def test_wechat_access_token_is_cached_within_ttl() -> None:
    http_client = FakeHTTPClient(
        [
            {"access_token": "token-1", "expires_in": 7200},
            {"errcode": 0, "errmsg": "ok"},
            {"errcode": 0, "errmsg": "ok"},
        ]
    )
    client = WeChatClient(
        app_id="wx-cache",
        app_secret="secret",
        client=http_client,
    )

    first = asyncio.run(
        client.send_subscribe_message(
            "openid-cache", TEMPLATE_ID, "pages/review/review", {}
        )
    )
    second = asyncio.run(
        client.send_subscribe_message(
            "openid-cache", TEMPLATE_ID, "pages/review/review", {}
        )
    )

    assert first.errcode == second.errcode == 0
    assert len(http_client.requests) == 3
    assert http_client.requests[0][0] == "GET"
    assert client._token == "token-1"
    assert 6990 <= client._token_expires_at - time.monotonic() <= 7000


def test_wechat_invalid_access_token_refreshes_and_retries_once() -> None:
    http_client = FakeHTTPClient(
        [
            {"access_token": "stale-token", "expires_in": 7200},
            {"errcode": 40001, "errmsg": "invalid credential"},
            {"access_token": "fresh-token", "expires_in": 7200},
            {"errcode": 0, "errmsg": "ok"},
        ]
    )
    client = WeChatClient(
        app_id="wx-refresh",
        app_secret="secret",
        client=http_client,
    )

    result = asyncio.run(client.send_subscribe_message("openid", TEMPLATE_ID, None, {}))

    assert result.errcode == 0
    assert client._token == "fresh-token"
    assert [
        request[2]["access_token"]
        for request in http_client.requests
        if request[0] == "POST"
    ] == [
        "stale-token",
        "fresh-token",
    ]


def test_wechat_timeout_is_classified_as_uncertain() -> None:
    http_client = FakeHTTPClient(
        [
            {"access_token": "token", "expires_in": 7200},
            httpx2.TimeoutException("send timed out"),
        ]
    )
    client = WeChatClient(
        app_id="wx-timeout",
        app_secret="secret",
        client=http_client,
    )

    try:
        asyncio.run(client.send_subscribe_message("openid", TEMPLATE_ID, None, {}))
    except WeChatClientError as exc:
        assert exc.uncertain is True
    else:
        raise AssertionError("timeout must raise WeChatClientError")


def test_dispatcher_sends_mapped_message_and_consumes_grant_once() -> None:
    with SessionLocal() as session:
        user_id, openid = _seed_user(session, label="worker-sent", due_cards=2)
        grant = _record_grant(session, user_id, "worker-sent-grant")
    wechat = FakeWeChatClient([WeChatSendResult(0, "ok")])
    dispatcher = ReminderDispatcher(
        session_factory=SessionLocal,
        settings=_worker_settings(),
        wechat_client=wechat,
    )

    claimed = asyncio.run(dispatcher.dispatch_once(now=FIXED_NOW))
    repeated = asyncio.run(dispatcher.dispatch_once(now=FIXED_NOW + timedelta(hours=1)))

    assert claimed == 1
    assert repeated == 0
    assert len(wechat.calls) == 1
    assert wechat.calls[0]["openid"] == openid
    assert wechat.calls[0]["template_id"] == TEMPLATE_ID
    assert wechat.calls[0]["page"] == "pages/review/review"
    assert wechat.calls[0]["data"] == {
        "number1": {"value": "2"},
        "time2": {"value": "2026-08-13 12:00"},
        "thing3": {"value": "今天有 2 个知识点建议复习"},
    }
    with SessionLocal() as session:
        delivery = session.scalar(
            select(ReminderDelivery).where(ReminderDelivery.user_id == user_id)
        )
        assert delivery.status == "sent"
        assert delivery.response_code == 0
        assert delivery.sent_at is not None
        persisted_grant = session.get(ReminderSubscriptionGrant, grant.id)
        assert persisted_grant is not None
        assert persisted_grant.status == "consumed"


def test_dispatcher_preserves_delivery_error_grant_semantics() -> None:
    with SessionLocal() as session:
        rejected_user, _ = _seed_user(session, label="worker-rejected", due_cards=1)
        failed_user, _ = _seed_user(session, label="worker-failed", due_cards=1)
        uncertain_user, _ = _seed_user(session, label="worker-timeout", due_cards=1)
        rejected_grant = _record_grant(session, rejected_user, "worker-rejected-grant")
        failed_grant = _record_grant(session, failed_user, "worker-failed-grant")
        uncertain_grant = _record_grant(session, uncertain_user, "worker-timeout-grant")
    wechat = FakeWeChatClient(
        [
            WeChatSendResult(43101, "user refuses"),
            WeChatSendResult(47003, "template argument invalid"),
            WeChatClientError("send timed out", uncertain=True),
        ]
    )
    dispatcher = ReminderDispatcher(
        session_factory=SessionLocal,
        settings=_worker_settings(),
        wechat_client=wechat,
    )

    assert asyncio.run(dispatcher.dispatch_once(now=FIXED_NOW)) == 3
    with SessionLocal() as session:
        statuses = {
            delivery.user_id: delivery.status
            for delivery in session.scalars(select(ReminderDelivery)).all()
            if delivery.user_id
            in {rejected_user, failed_user, uncertain_user}
        }
        assert statuses == {
            rejected_user: "failed",
            failed_user: "failed",
            uncertain_user: "uncertain",
        }
        persisted_rejected = session.get(ReminderSubscriptionGrant, rejected_grant.id)
        persisted_failed = session.get(ReminderSubscriptionGrant, failed_grant.id)
        persisted_uncertain = session.get(ReminderSubscriptionGrant, uncertain_grant.id)
        assert persisted_rejected is not None
        assert persisted_failed is not None
        assert persisted_uncertain is not None
        assert persisted_rejected.status == "invalid"
        assert persisted_failed.status == "available"
        assert persisted_uncertain.status == "reserved"


def test_dispatcher_does_not_claim_before_preferred_time_or_without_credentials() -> None:
    with SessionLocal() as session:
        user_id, _ = _seed_user(session, label="worker-not-due", due_cards=1)
        grant = _record_grant(session, user_id, "worker-not-due-grant")
        preference = session.scalar(
            select(ReminderPreference).where(ReminderPreference.user_id == user_id)
        )
        preference.preferred_time = "23:59"
        session.commit()
    wechat = FakeWeChatClient([])
    dispatcher = ReminderDispatcher(
        session_factory=SessionLocal,
        settings=_worker_settings(),
        wechat_client=wechat,
    )

    assert asyncio.run(dispatcher.dispatch_once(now=FIXED_NOW)) == 0
    with SessionLocal() as session:
        persisted_grant = session.get(ReminderSubscriptionGrant, grant.id)
        assert persisted_grant is not None
        assert persisted_grant.status == "available"
        assert (
            session.scalar(
                select(ReminderDelivery).where(ReminderDelivery.user_id == user_id)
            )
            is None
        )

    without_secret = ReminderDispatcher(
        session_factory=SessionLocal,
        settings=_worker_settings(wechat_app_secret=None),
        wechat_client=wechat,
    )
    assert (
        asyncio.run(without_secret.dispatch_once(now=FIXED_NOW + timedelta(minutes=1)))
        == 0
    )


def test_worker_recovers_after_a_tick_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    stop_event = threading.Event()
    dispatcher = ReminderDispatcher(
        session_factory=SessionLocal,
        settings=_worker_settings(),
        wechat_client=FakeWeChatClient([]),
    )
    calls = 0

    async def return_immediately(_func, *_args):
        return None

    monkeypatch.setattr(asyncio, "to_thread", return_immediately)

    async def replace_dispatch_once() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated worker failure")
        stop_event.set()
        return 0

    dispatcher.dispatch_once = replace_dispatch_once
    asyncio.run(dispatcher.run_until_stopped(stop_event))
    assert calls == 2
