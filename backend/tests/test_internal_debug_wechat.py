import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import memory_agent.api as api
from memory_agent.config import Settings
from memory_agent.wechat import WeChatSendResult


def _request(token: str | None) -> Request:
    headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
    return Request({"type": "http", "headers": headers})


def _settings(token: str | None) -> Settings:
    return Settings(
        debug_wechat_token=token,
        wechat_app_id="wx-test",
        wechat_app_secret="secret",
        wechat_subscribe_template_id="configured-template",
        wechat_template_field_map_json={
            "plan_name": "thing2", "reminder_time": "time1", "summary": "thing3"
        },
    )


def test_debug_endpoint_is_closed_without_token_or_with_wrong_token() -> None:
    body = api.DebugWechatReminderRequest(openid="openid")
    for settings, request in [(_settings(None), _request(None)), (_settings("ok"), _request("bad"))]:
        with pytest.raises(HTTPException) as error:
            asyncio.run(api.debug_wechat_reminder(body, request, settings))
        assert error.value.status_code in {403, 404}


def test_debug_endpoint_builds_payload_without_database_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def send_subscribe_message(self, openid, template_id, page, data):
            calls.append((openid, template_id, data))
            return WeChatSendResult(0, "ok")
        async def aclose(self):
            pass

    monkeypatch.setattr(api, "WeChatClient", FakeClient)
    result = asyncio.run(api.debug_wechat_reminder(
        api.DebugWechatReminderRequest(openid="openid", due_count=13),
        _request("ok"), _settings("ok"),
    ))
    assert result["payload"] == {
        "thing2": {"value": "今日知识复习"},
        "time1": {"value": "2026-08-13 12:00"},
        "thing3": {"value": "今天有 13 个知识点建议复习"},
    }
    assert result["diagnostics"]["database_mutations"] is False
    assert calls[0][1] == "configured-template"


def test_debug_endpoint_returns_wechat_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, **kwargs): pass
        async def send_subscribe_message(self, *args): return WeChatSendResult(47003, "invalid template argument")
        async def aclose(self): pass
    monkeypatch.setattr(api, "WeChatClient", FakeClient)
    result = asyncio.run(api.debug_wechat_reminder(
        api.DebugWechatReminderRequest(openid="openid", template_id="request-template"),
        _request("ok"), _settings("ok"),
    ))
    assert result["wechat"] == {"errcode": 47003, "errmsg": "invalid template argument"}
