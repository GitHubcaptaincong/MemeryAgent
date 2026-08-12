from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from memory_agent.database import SessionLocal
from memory_agent.main import app
from memory_agent.models import (
    AgentCheckpoint,
    MemoryItem,
    ReminderPreference,
    RetrievalDocument,
    ReviewCard,
    ReviewEvent,
)


def test_readiness_checks_database_connection() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_source_to_confirmed_draft_and_approved_memory() -> None:
    with TestClient(app) as client:
        source_response = client.post(
            "/api/v1/sources",
            json={
                "title": "费曼学习法",
                "learning_goal": "理解如何用主动解释发现知识盲区",
                "content_type": "markdown",
                "web_access_allowed": False,
                "content": (
                    "费曼学习法要求学习者先选择一个概念，并尝试用简单语言解释它。\n\n"
                    "当解释出现含糊或卡顿时，这些位置通常就是知识盲区。学习者应回到材料，"
                    "补齐理解后再次解释。\n\n"
                    "最后要继续简化表达，并用例子验证自己是否真的理解。"
                ),
            },
        )
        assert source_response.status_code == 201, source_response.text
        source = source_response.json()
        assert source["char_count"] > 50

        run_response = client.post(
            "/api/v1/runs",
            json={"source_id": source["id"], "idempotency_key": "test-flow-0001"},
        )
        assert run_response.status_code == 202, run_response.text
        run_id = run_response.json()["id"]

        run = client.get(f"/api/v1/runs/{run_id}")
        assert run.status_code == 200
        assert run.json()["state"] == "awaiting_user"
        assert run.json()["tool_call_count"] == 2

        events = client.get(f"/api/v1/runs/{run_id}/events").json()
        assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
        assert any(event["event_type"] == "skills.selected" for event in events)
        assert any(event["event_type"] == "tool.completed" for event in events)
        assert any(event["event_type"] == "draft.created" for event in events)
        assert any(event["event_type"] == "checkpoint.created" for event in events)

        draft_response = client.get(f"/api/v1/runs/{run_id}/draft")
        assert draft_response.status_code == 200, draft_response.text
        draft = draft_response.json()
        assert 1 <= len(draft["units"]) <= 10
        assert draft["units"][0]["evidence"][0]["evidence_type"] == "source_span"

        confirm_response = client.post(f"/api/v1/drafts/{draft['id']}/confirm")
        assert confirm_response.status_code == 200, confirm_response.text
        assert confirm_response.json()["status"] == "confirmed"
        assert client.get(f"/api/v1/runs/{run_id}").json()["state"] == "completed"
        confirmed_events = client.get(f"/api/v1/runs/{run_id}/events").json()
        assert confirmed_events[-1]["payload"]["state"] == "completed"
        assert any(event["event_type"] == "review.cards_created" for event in confirmed_events)
        assert any(event["event_type"] == "memory.candidate_created" for event in confirmed_events)

        review_queue = client.get("/api/v1/review/queue").json()
        assert len(review_queue) == len(draft["units"])
        first_card = review_queue[0]
        assert first_card["scheduler_version"] == "fsrs-6.3.1-v1"
        assert [item["rating"] for item in first_card["rating_options"]] == [1, 2, 3, 4]
        answer_response = client.post(
            f"/api/v1/review/cards/{first_card['id']}/answers",
            json={
                "answer": "主动解释时出现的卡顿会暴露理解缺口，需要回到材料补齐。",
                "idempotency_key": "review-answer-flow-0001",
            },
        )
        assert answer_response.status_code == 201, answer_response.text
        answer = answer_response.json()
        assert answer["evaluation_status"] == "self_rating_required"
        assert answer["answer_key"]
        conflicting_answer = client.post(
            f"/api/v1/review/cards/{first_card['id']}/answers",
            json={
                "answer": "同一个幂等键不能改成另一份回答。",
                "idempotency_key": "review-answer-flow-0001",
            },
        )
        assert conflicting_answer.status_code == 409
        second_attempt = client.post(
            f"/api/v1/review/cards/{first_card['id']}/answers",
            json={
                "answer": "这是同一轮中尚未评分的第二次尝试。",
                "idempotency_key": "review-answer-flow-0002",
            },
        ).json()
        rating_payload = {
            "attempt_id": answer["attempt_id"],
            "rating": 3,
            "idempotency_key": "review-rating-flow-0001",
        }
        rating_response = client.post(
            f"/api/v1/review/cards/{first_card['id']}/ratings",
            json=rating_payload,
        )
        assert rating_response.status_code == 200, rating_response.text
        assert rating_response.json()["user_rating_is_final"] is True
        assert rating_response.json()["interval_days"] > 0
        assert rating_response.json()["scheduler_version"] == "fsrs-6.3.1-v1"
        assert rating_response.json()["scheduler_state"]["algorithm"] == "fsrs-6"
        duplicate_rating = client.post(
            f"/api/v1/review/cards/{first_card['id']}/ratings",
            json=rating_payload,
        )
        assert duplicate_rating.status_code == 200
        conflicting_rating = client.post(
            f"/api/v1/review/cards/{first_card['id']}/ratings",
            json={**rating_payload, "rating": 2},
        )
        assert conflicting_rating.status_code == 409
        stale_rating = client.post(
            f"/api/v1/review/cards/{first_card['id']}/ratings",
            json={
                "attempt_id": second_attempt["attempt_id"],
                "rating": 3,
                "idempotency_key": "review-rating-flow-0002",
            },
        )
        assert stale_rating.status_code == 409
        assert len(client.get("/api/v1/review/queue").json()) == len(draft["units"]) - 1

        overview = client.get("/api/v1/review/overview")
        assert overview.status_code == 200
        assert overview.json()["due_count"] == len(draft["units"]) - 1
        assert overview.json()["total_active"] == len(draft["units"])
        assert overview.json()["next_due_at"] is not None
        history = client.get("/api/v1/review/history?limit=5")
        assert history.status_code == 200
        assert history.json()[0]["card_id"] == first_card["id"]
        assert history.json()[0]["rating"] == 3

        reminder = client.get("/api/v1/reminders/preferences")
        assert reminder.status_code == 200
        updated_reminder = client.put(
            "/api/v1/reminders/preferences",
            json={
                "enabled": True,
                "preferred_time": "19:30",
                "daily_limit": 8,
                "overdue_enabled": True,
                "timezone": "Asia/Shanghai",
            },
        )
        assert updated_reminder.status_code == 200
        assert updated_reminder.json()["preferred_time"] == "19:30"

        candidates = client.get("/api/v1/memory-candidates?status=pending").json()
        assert len(candidates) == 1
        candidate_id = candidates[0]["id"]
        decision_response = client.post(
            f"/api/v1/memory-candidates/{candidate_id}/decision",
            json={"decision": "approve"},
        )
        assert decision_response.status_code == 200, decision_response.text
        assert decision_response.json()["status"] == "approved"

        session = SessionLocal()
        try:
            assert session.scalar(select(func.count(AgentCheckpoint.id))) == 1
            assert session.scalar(select(func.count(MemoryItem.id))) == 1
            assert session.scalar(select(func.count(RetrievalDocument.id))) == 1
            assert session.scalar(select(func.count(ReviewCard.id))) == len(draft["units"])
            assert session.scalar(select(func.count(ReviewEvent.id))) == 3
            assert session.scalar(select(func.count(ReminderPreference.id))) == 1
        finally:
            session.close()


def test_run_idempotency_returns_the_same_run() -> None:
    with TestClient(app) as client:
        source = client.post(
            "/api/v1/sources",
            json={
                "title": "幂等测试",
                "learning_goal": "验证重复请求不会创建重复 Agent 任务",
                "content": "幂等键用于识别同一个创建请求，服务端应返回第一次创建的任务。",
            },
        ).json()
        payload = {"source_id": source["id"], "idempotency_key": "same-request-0001"}
        first = client.post("/api/v1/runs", json=payload)
        second = client.post("/api/v1/runs", json=payload)
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
