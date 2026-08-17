from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import memory_agent.api as api_module
from memory_agent.main import app
from memory_agent.source_ingestion import (
    FetchedSource,
    HttpResponse,
    SourceFetchError,
    detect_standalone_url,
    extract_readable_content,
    fetch_public_source,
)


def test_detect_standalone_url_only_opens_an_explicit_single_link() -> None:
    assert detect_standalone_url("  https://example.com/article  ") == "https://example.com/article"
    assert detect_standalone_url("请参考 https://example.com/article") is None
    assert detect_standalone_url("不是链接") is None


def test_extract_readable_html_ignores_scripts_and_keeps_structure() -> None:
    title, content = extract_readable_content(
        """
        <html><head><title>  学习 页面 </title><script>secret()</script></head>
        <body><main><h1>检索练习</h1><p>先回忆，再核对答案。</p>
        <style>.hidden { display: none }</style><ul><li>主动提取</li><li>及时反馈</li></ul>
        </main></body></html>
        """,
        "text/html",
    )

    assert title == "学习 页面"
    assert content == "检索练习\n先回忆，再核对答案。\n主动提取\n及时反馈"
    assert "secret" not in content
    assert "display" not in content


def test_fetch_public_source_rejects_private_target_before_request() -> None:
    requested = False

    def requester(*args, **kwargs):
        nonlocal requested
        requested = True
        raise AssertionError("private targets must not reach the transport")

    with pytest.raises(SourceFetchError) as raised:
        fetch_public_source(
            "http://127.0.0.1/private",
            max_chars=50_000,
            requester=requester,
        )

    assert raised.value.code == "private_target_blocked"
    assert requested is False


def test_fetch_public_source_revalidates_redirect_targets() -> None:
    def resolver(host: str, port: int) -> list[str]:
        if host == "public.example":
            return ["93.184.216.34"]
        raise SourceFetchError("private_target_blocked", "blocked")

    def requester(url: str, connect_ip: str, **kwargs) -> HttpResponse:
        assert connect_ip == "93.184.216.34"
        return HttpResponse(302, {"location": "http://127.0.0.1/admin"}, b"")

    with pytest.raises(SourceFetchError) as raised:
        fetch_public_source(
            "https://public.example/article",
            max_chars=50_000,
            resolver=resolver,
            requester=requester,
        )

    assert raised.value.code == "private_target_blocked"


def test_long_text_and_public_url_sources_keep_origin_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    retrieved_at = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
    public_content = (
        "间隔复习需要根据回忆表现动态安排下一次练习。"
        "主动回忆比重复阅读更能暴露知识缺口。"
    ) * 12

    monkeypatch.setattr(
        api_module,
        "fetch_public_source",
        lambda *args, **kwargs: FetchedSource(
            requested_url="https://example.com/original",
            final_url="https://example.com/learning",
            title="公开学习材料",
            content=public_content,
            content_type="text/html",
            retrieved_at=retrieved_at,
            response_hash="a" * 64,
        ),
    )

    with TestClient(app) as client:
        long_text = "长文本知识段落。" * 1_500
        long_response = client.post(
            "/api/v1/sources/resolve",
            json={
                "title": "长文本",
                "learning_goal": "验证超过旧上限的材料可以保存",
                "input": long_text,
                "content_type": "text",
                "web_access_allowed": False,
            },
        )
        assert long_response.status_code == 201, long_response.text
        assert long_response.json()["char_count"] == len(long_text)
        assert long_response.json()["origin_type"] == "text"

        too_long_response = client.post(
            "/api/v1/sources",
            json={
                "title": "超长文本",
                "learning_goal": "验证上限",
                "content": "字" * 50_001,
                "content_type": "text",
                "web_access_allowed": False,
            },
        )
        assert too_long_response.status_code == 422

        source_response = client.post(
            "/api/v1/sources/resolve",
            json={
                "input": "https://example.com/original",
                "learning_goal": "理解主动回忆与间隔复习",
                "web_access_allowed": False,
            },
        )
        assert source_response.status_code == 201, source_response.text
        source = source_response.json()
        assert source["origin_type"] == "url"
        assert source["origin_url"] == "https://example.com/learning"
        assert source["retrieved_at"] == "2026-08-17T08:00:00Z"
        assert source["origin_content_hash"] == "a" * 64

        run_response = client.post(
            "/api/v1/runs",
            json={"source_id": source["id"], "idempotency_key": "url-source-test-0001"},
        )
        assert run_response.status_code == 202, run_response.text
        run_id = run_response.json()["id"]
        draft_response = client.get(f"/api/v1/runs/{run_id}/draft")
        assert draft_response.status_code == 200, draft_response.text
        evidence = draft_response.json()["units"][0]["evidence"][0]
        assert evidence["url"] == "https://example.com/learning"
        assert evidence["retrieved_at"] == "2026-08-17T08:00:00"
