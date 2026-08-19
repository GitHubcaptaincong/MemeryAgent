from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from memory_agent.database import SessionLocal
from memory_agent.main import app
from memory_agent.models import DraftUnit, KnowledgeDraft, ReviewCard, ReviewEvent


def _confirmed_set(client: TestClient, *, title: str, key: str) -> dict:
    source = client.post(
        "/api/v1/sources",
        json={
            "title": title,
            "learning_goal": f"理解 {title}",
            "content": (
                f"{title} 的第一个核心概念用于解释基础机制。\n\n"
                f"{title} 的第二个核心概念用于解释实践中的取舍和边界。"
            ),
            "content_type": "text",
            "web_access_allowed": False,
        },
    ).json()
    run = client.post(
        "/api/v1/runs",
        json={"source_id": source["id"], "idempotency_key": key},
    ).json()
    draft = client.get(f"/api/v1/runs/{run['id']}/draft").json()
    confirmed = client.post(f"/api/v1/drafts/{draft['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def test_knowledge_set_crud_stats_and_review_filtering() -> None:
    with TestClient(app) as client:
        first = _confirmed_set(client, title="知识管理第一组", key="knowledge-set-test-0001")
        second = _confirmed_set(client, title="知识管理第二组", key="knowledge-set-test-0002")

        listing = client.get("/api/v1/knowledge-sets")
        assert listing.status_code == 200, listing.text
        by_id = {item["id"]: item for item in listing.json()}
        assert first["id"] in by_id and second["id"] in by_id
        assert by_id[first["id"]]["title"] == "知识管理第一组"
        assert by_id[first["id"]]["review_count"] == 0
        assert by_id[first["id"]]["last_reviewed_at"] is None

        detail = client.get(f"/api/v1/knowledge-sets/{first['id']}").json()
        assert detail["unit_count"] == len(first["units"])
        unit = detail["units"][0]
        card = next(
            item
            for item in client.get(
                f"/api/v1/review/queue?limit=100&knowledge_set_id={first['id']}"
            ).json()
            if item["draft_unit_id"] == unit["id"]
        )
        answer = client.post(
            f"/api/v1/review/cards/{card['id']}/answers",
            json={"answer": "测试回答", "idempotency_key": "knowledge-answer-0001"},
        ).json()
        rated = client.post(
            f"/api/v1/review/cards/{card['id']}/ratings",
            json={
                "attempt_id": answer["attempt_id"],
                "rating": 3,
                "idempotency_key": "knowledge-rating-0001",
            },
        )
        assert rated.status_code == 200, rated.text

        with SessionLocal() as session:
            event_count = session.scalar(
                select(func.count(ReviewEvent.id)).where(ReviewEvent.card_id == UUID(card["id"]))
            )
        updated = client.patch(
            f"/api/v1/knowledge-units/{unit['id']}",
            json={"question": "更新后的问题", "answer": "要点一\n要点二"},
        )
        assert updated.status_code == 200, updated.text
        updated_unit = next(item for item in updated.json()["units"] if item["id"] == unit["id"])
        assert updated_unit["question"] == "更新后的问题"
        assert updated_unit["answer"] == "要点一\n要点二"
        with SessionLocal() as session:
            assert session.scalar(
                select(func.count(ReviewEvent.id)).where(ReviewEvent.card_id == UUID(card["id"]))
            ) == event_count

        renamed = client.patch(
            f"/api/v1/knowledge-sets/{first['id']}", json={"title": "修改后的知识集"}
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "修改后的知识集"
        assert renamed.json()["review_count"] == 1
        assert renamed.json()["last_reviewed_at"] is not None

        deleted_unit = client.delete(f"/api/v1/knowledge-units/{unit['id']}")
        assert deleted_unit.status_code == 204
        with SessionLocal() as session:
            assert session.get(DraftUnit, UUID(unit["id"])).status == "archived"
            assert session.get(ReviewCard, UUID(card["id"])).status == "archived"
        assert all(
            item["draft_unit_id"] != unit["id"]
            for item in client.get("/api/v1/review/queue?limit=100").json()
        )

        deleted_set = client.delete(f"/api/v1/knowledge-sets/{second['id']}")
        assert deleted_set.status_code == 204
        with SessionLocal() as session:
            assert session.get(KnowledgeDraft, UUID(second["id"])).status == "archived"
        assert client.get(f"/api/v1/knowledge-sets/{second['id']}").status_code == 404
        assert client.get(
            f"/api/v1/review/queue?limit=100&knowledge_set_id={second['id']}"
        ).json() == []
        assert client.delete(f"/api/v1/knowledge-sets/{first['id']}").status_code in {204, 404}
