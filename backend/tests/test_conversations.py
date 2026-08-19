from __future__ import annotations

from fastapi.testclient import TestClient

from memory_agent.main import app


def test_conversation_persists_turn_and_gets_ai_title_after_first_draft() -> None:
    with TestClient(app) as client:
        conversation_response = client.post("/api/v1/conversations")
        assert conversation_response.status_code == 201, conversation_response.text
        conversation = conversation_response.json()
        assert conversation["title"] == "新对话"
        assert conversation["title_status"] == "pending"

        empty_conversation = client.post("/api/v1/conversations").json()
        assert client.get("/api/v1/conversations").json() == []
        history_with_empty = client.get(
            "/api/v1/conversations?include_empty=true"
        ).json()
        assert {item["id"] for item in history_with_empty} == {
            conversation["id"],
            empty_conversation["id"],
        }

        turn_payload = {
            "input": (
                "主动回忆要求学习者先尝试从记忆中回答问题，再查看材料核对。"
                "这种练习能暴露尚未掌握的知识缺口。"
            ),
            "content_type": "text",
            "web_access_allowed": False,
            "idempotency_key": "conversation-turn-test-0001",
        }
        turn_response = client.post(
            f"/api/v1/conversations/{conversation['id']}/turns",
            json=turn_payload,
        )
        assert turn_response.status_code == 202, turn_response.text
        started = turn_response.json()
        assert started["turn"]["position"] == 1
        assert started["turn"]["user_content"] == turn_payload["input"]
        assert started["turn"]["source_type"] == "text"
        assert started["turn"]["source_url"] is None

        detail_response = client.get(f"/api/v1/conversations/{conversation['id']}")
        assert detail_response.status_code == 200, detail_response.text
        detail = detail_response.json()
        assert detail["conversation"]["title_status"] == "generated"
        assert detail["conversation"]["title"] != "新对话"
        assert detail["conversation"]["turn_count"] == 1
        assert detail["turns"][0]["run_state"] == "awaiting_user"
        assert detail["turns"][0]["assistant_summary"]
        assert detail["turns"][0]["draft"]["units"]

        duplicate_response = client.post(
            f"/api/v1/conversations/{conversation['id']}/turns",
            json=turn_payload,
        )
        assert duplicate_response.status_code == 202, duplicate_response.text
        assert duplicate_response.json()["turn"]["id"] == started["turn"]["id"]
        assert client.get(f"/api/v1/conversations/{conversation['id']}").json()[
            "conversation"
        ]["turn_count"] == 1

        second_payload = {
            **turn_payload,
            "input": "主动回忆与重复阅读有什么区别？",
            "idempotency_key": "conversation-turn-test-0002",
        }
        second_response = client.post(
            f"/api/v1/conversations/{conversation['id']}/turns",
            json=second_payload,
        )
        assert second_response.status_code == 202, second_response.text

        first_page = client.get(
            f"/api/v1/conversations/{conversation['id']}/turns?limit=1"
        )
        assert first_page.status_code == 200, first_page.text
        assert [item["position"] for item in first_page.json()["items"]] == [1]
        assert first_page.json()["next_after_position"] == 1

        second_page = client.get(
            f"/api/v1/conversations/{conversation['id']}/turns?after_position=1&limit=1"
        )
        assert second_page.status_code == 200, second_page.text
        assert [item["position"] for item in second_page.json()["items"]] == [2]
        assert second_page.json()["next_after_position"] is None

        rename_response = client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            json={"title": "主动回忆复习"},
        )
        assert rename_response.status_code == 200, rename_response.text
        assert rename_response.json()["title"] == "主动回忆复习"
        assert rename_response.json()["title_status"] == "custom"

        conversations = client.get("/api/v1/conversations").json()
        saved = next(item for item in conversations if item["id"] == conversation["id"])
        assert saved["title_status"] == "custom"
        assert saved["turn_count"] == 2
        assert len(client.get("/api/v1/conversations?limit=1&offset=0").json()) == 1

        delete_response = client.delete(
            f"/api/v1/conversations/{conversation['id']}"
        )
        assert delete_response.status_code == 204, delete_response.text
        assert client.get(f"/api/v1/conversations/{conversation['id']}").status_code == 404
