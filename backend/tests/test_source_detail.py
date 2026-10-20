from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from memory_agent.config import Settings, get_settings
from memory_agent.database import SessionLocal
from memory_agent.main import app
from memory_agent.models import Source


def test_source_snapshot_is_exact_and_scoped_to_owner():
    settings = Settings(
        auth_mode="wechat", wechat_app_id="wx_source_detail_test",
        wechat_claim_local_user=False, model_provider="fake",
    )
    owner = {"X-WX-APPID": settings.wechat_app_id, "X-WX-OPENID": "source_detail_owner"}
    other = {**owner, "X-WX-OPENID": "source_detail_other"}
    content = "前😀文\n可定位的原文。\n重复引用。重复引用。"
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            created = client.post("/api/v1/sources", headers=owner, json={
                "title": "原文定位测试", "learning_goal": "验证证据", "content": content,
            })
            assert created.status_code == 201, created.text
            source_id = created.json()["id"]
            url = f"/api/v1/sources/{source_id}"
            response = client.get(url, headers=owner)
            assert response.status_code == 200, response.text
            assert response.json()["raw_content"] == content
            assert response.json()["char_count"] == len(content)
            assert client.get(url).status_code == 401
            assert client.get(url, headers=other).status_code == 404
            assert client.get(f"/api/v1/sources/{uuid4()}", headers=owner).status_code == 404
            assert client.get("/api/v1/sources/not-a-uuid", headers=owner).status_code == 422
            with SessionLocal() as session:
                source = session.get(Source, UUID(source_id))
                source.deleted_at = datetime.now(UTC)
                session.commit()
            assert client.get(url, headers=owner).status_code == 404
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_saved_evidence_and_history_remain_readable_after_confirmation():
    with TestClient(app) as client:
        source = client.post("/api/v1/sources", json={
            "title": "证据回看", "learning_goal": "理解证据回看",
            "content": "知识点通过原文引用支持。\n\n复习通过主动回忆来巩固理解。",
        }).json()
        run = client.post("/api/v1/runs", json={
            "source_id": source["id"], "idempotency_key": "source-detail-history-0001",
        }).json()
        draft = client.get(f"/api/v1/runs/{run['id']}/draft").json()
        assert client.post(f"/api/v1/drafts/{draft['id']}/confirm").status_code == 200
        snapshot = client.get(f"/api/v1/sources/{source['id']}").json()
        for unit in draft["units"]:
            for evidence in unit["evidence"]:
                assert snapshot["raw_content"][evidence["start_char"]:evidence["end_char"]] == evidence["quote"]
        events = client.get(f"/api/v1/runs/{run['id']}/events?after_seq=0").json()
        assert any(event["event_type"] == "draft.created" for event in events)
        assert any(event["event_type"] == "tool.completed" for event in events)
        assert all(event["visible_to_user"] for event in events)
