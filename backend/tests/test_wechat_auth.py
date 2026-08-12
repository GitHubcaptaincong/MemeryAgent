from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from memory_agent.config import Settings, get_settings
from memory_agent.database import SessionLocal
from memory_agent.main import app
from memory_agent.models import Source, WechatIdentity


APP_ID = "wx1234567890abcdef"
OWNER_HEADERS = {
    "X-WX-APPID": APP_ID,
    "X-WX-OPENID": "owner_openid_123456",
    "X-WX-SOURCE": "wx_devtools",
}
OTHER_HEADERS = {
    "X-WX-APPID": APP_ID,
    "X-WX-OPENID": "other_openid_123456",
    "X-WX-SOURCE": "wx_client",
}


def _wechat_settings() -> Settings:
    return Settings(
        auth_mode="wechat",
        wechat_app_id=APP_ID,
        wechat_claim_local_user=True,
        model_provider="fake",
    )


def test_wechat_headers_are_required_and_app_id_is_checked() -> None:
    app.dependency_overrides[get_settings] = _wechat_settings
    try:
        with TestClient(app) as client:
            missing = client.get("/api/v1/review/overview")
            wrong_app = client.get(
                "/api/v1/review/overview",
                headers={**OWNER_HEADERS, "X-WX-APPID": "wxwrongappid0000"},
            )
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert missing.status_code == 401
    assert wrong_app.status_code == 403


def test_first_wechat_user_claims_legacy_data_and_users_are_isolated() -> None:
    settings = _wechat_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            owner_source_response = client.post(
                "/api/v1/sources",
                headers=OWNER_HEADERS,
                json={
                    "title": "Owner-only source",
                    "learning_goal": "Verify OpenID isolation",
                    "content": "This source must remain visible only to its owning WeChat user.",
                },
            )
            assert owner_source_response.status_code == 201, owner_source_response.text
            owner_source_id = owner_source_response.json()["id"]

            other_overview = client.get("/api/v1/review/overview", headers=OTHER_HEADERS)
            assert other_overview.status_code == 200, other_overview.text
            assert other_overview.json() == {
                "due_count": 0,
                "total_active": 0,
                "next_due_at": None,
            }

            cross_user_run = client.post(
                "/api/v1/runs",
                headers=OTHER_HEADERS,
                json={
                    "source_id": owner_source_id,
                    "idempotency_key": "cross-user-run-0001",
                },
            )
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert cross_user_run.status_code == 404

    with SessionLocal() as session:
        owner_identity = session.scalar(
            select(WechatIdentity).where(
                WechatIdentity.app_id == APP_ID,
                WechatIdentity.openid == OWNER_HEADERS["X-WX-OPENID"],
            )
        )
        other_identity = session.scalar(
            select(WechatIdentity).where(
                WechatIdentity.app_id == APP_ID,
                WechatIdentity.openid == OTHER_HEADERS["X-WX-OPENID"],
            )
        )
        source = session.get(Source, UUID(owner_source_id))

        assert owner_identity is not None
        assert owner_identity.user_id == settings.local_user_id
        assert other_identity is not None
        assert other_identity.user_id != owner_identity.user_id
        assert source is not None
        assert source.user_id == owner_identity.user_id
