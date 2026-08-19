from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from memory_agent.agent import AgentRuntime
from memory_agent.cli_proxy_adapter import (
    CLIProxyResponsesAdapter,
    ModelProtocolError,
    ModelProviderError,
)
from memory_agent.config import Settings
from memory_agent.draft_contract import canonical_draft_hash, validate_draft_payload
from memory_agent.model_adapters import AgentModelContext
from memory_agent.tools import build_tool_registry, source_locate_quotes


SOURCE = "主动解释能暴露知识盲区。发现卡顿后，应回到材料补齐理解，再重新解释。"


def _draft() -> dict[str, Any]:
    quote = "主动解释能暴露知识盲区。"
    return {
        "learning_goal": "理解主动解释如何发现知识盲区",
        "agent_summary": {
            "overview": "材料强调通过主动解释定位并修补知识盲区。",
            "generation_mode": "cli_proxy",
            "requires_user_confirmation": True,
        },
        "units": [
            {
                "position": 1,
                "title": "用主动解释暴露盲区",
                "learning_objective": "能够解释主动说明为何能检测理解缺口。",
                "explanation": "当学习者尝试主动解释时，卡顿和含糊会暴露尚未理解的位置。",
                "key_points": ["主动解释", "卡顿对应知识盲区"],
                "question": "为什么主动解释能够帮助发现知识盲区？",
                "answer_key": ["解释时的卡顿和含糊会暴露理解缺口"],
                "hints": ["关注解释不顺畅的位置"],
                "tags": ["主动学习"],
                "applicable_scenarios": ["复盘技术概念"],
                "confidence": 0.9,
                "requires_user_confirmation": True,
                "uncertainties": [],
                "evidence": [
                    {
                        "evidence_type": "source_span",
                        "start_char": 0,
                        "end_char": len(quote),
                        "quote": quote,
                    }
                ],
            }
        ],
    }


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self.payload


class RecordingClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def post(self, _url: str, *, json: dict[str, Any]) -> FakeResponse:
        self.requests.append(json)
        return FakeResponse(self.responses.pop(0))


def _response(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"resp-{call_id}",
        "status": "completed",
        "output": [
            {"type": "reasoning", "encrypted_content": "opaque-provider-state"},
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": 10},
        },
    }


def _context() -> AgentModelContext:
    return AgentModelContext(
        source_title="主动学习笔记",
        learning_goal="理解主动解释如何发现知识盲区",
        source_char_count=len(SOURCE),
        profile_summary="用户偏好通过开放问题检查理解。",
        retrieved_memories=[],
        selected_skills=[
            {
                "name": "knowledge-decomposition",
                "version": "1.0.0",
                "description": "拆分知识单元",
                "content": "每个单元只承担一个主要学习目标。",
            }
        ],
        tools=build_tool_registry().definitions(),
        source_content=SOURCE,
    )


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        model_provider="cli_proxy",
        model_base_url="http://proxy.test/v1",
        model_api_key="test-only-secret",
        model_name="gpt-5.4-mini",
        model_quick_source_max_chars=0,
    )


def test_cli_proxy_runs_three_model_calls_then_finalizes_locally() -> None:
    draft = _draft()
    client = RecordingClient(
        [
            _response(
                "call-read",
                "source_read",
                {"start_char": 0, "end_char": len(SOURCE)},
            ),
            _response(
                "call-locate",
                "source_locate_quotes",
                {"quotes": [draft["units"][0]["evidence"][0]["quote"]]},
            ),
            _response("call-validate", "schema_validate", {"draft": draft}),
        ]
    )
    adapter = CLIProxyResponsesAdapter(_settings(), client=client)
    context = _context()

    first = adapter.next_step(context)
    assert first.kind == "tool_calls"
    assert first.tool_calls[0].name == "source_read"
    assert first.usage and first.usage.reasoning_tokens == 10
    context.tool_results.append(
        {"call_id": "call-read", "tool": "source_read", "result": {"content": SOURCE}}
    )

    second = adapter.next_step(context)
    assert second.tool_calls[0].name == "source_locate_quotes"
    assert any(
        item.get("type") == "function_call_output" and item.get("call_id") == "call-read"
        for item in client.requests[1]["input"]
    )
    quote = draft["units"][0]["evidence"][0]["quote"]
    context.tool_results.append(
        {
            "call_id": "call-locate",
            "tool": "source_locate_quotes",
            "result": {
                "requested_count": 1,
                "all_resolved": True,
                "resolved": [{"quote": quote, "start_char": 0, "end_char": len(quote)}],
                "unresolved": [],
            },
        }
    )

    third = adapter.next_step(context)
    assert third.tool_calls[0].name == "schema_validate"
    context.tool_results.append(
        {
            "call_id": "call-validate",
            "tool": "schema_validate",
            "result": {
                "valid": True,
                "errors": [],
                "draft_hash": canonical_draft_hash(draft),
            },
        }
    )

    final = adapter.next_step(context)
    assert final.kind == "final"
    assert final.final_draft == draft
    assert len(client.requests) == 3
    assert len(context.scratch["provider_items"]) == 9


def test_cached_draft_must_match_the_validated_draft() -> None:
    draft = _draft()
    changed = _draft()
    changed["units"][0]["title"] = "未经重新校验的标题"
    client = RecordingClient([])
    adapter = CLIProxyResponsesAdapter(_settings(), client=client)
    context = _context()
    context.tool_results.extend(
        [
            {"call_id": "read", "tool": "source_read", "result": {"content": SOURCE}},
            {
                "call_id": "validate",
                "tool": "schema_validate",
                "result": {
                    "valid": True,
                    "errors": [],
                    "draft_hash": canonical_draft_hash(draft),
                },
            },
        ]
    )
    context.scratch["validated_draft"] = changed
    try:
        adapter.next_step(context)
    except ModelProtocolError as exc:
        assert "cached draft differs from the validated draft" in str(exc)
    else:
        raise AssertionError("changed draft should have been rejected")


def test_runtime_rejects_persisted_validation_hash_mismatch() -> None:
    draft = _draft()
    try:
        AgentRuntime._validated_draft_payload(
            None,
            SimpleNamespace(id="run-hash-test"),
            arguments={"draft": draft},
            validation={"valid": True, "errors": [], "draft_hash": "invalid"},
        )
    except RuntimeError as exc:
        assert "hash does not match" in str(exc)
    else:
        raise AssertionError("persisted schema arguments must match their validation hash")


def test_invalid_schema_result_stays_in_correction_flow() -> None:
    corrected = _draft()
    client = RecordingClient(
        [_response("call-corrected-validate", "schema_validate", {"draft": corrected})]
    )
    adapter = CLIProxyResponsesAdapter(_settings(), client=client)
    context = _context()
    quote = corrected["units"][0]["evidence"][0]["quote"]
    context.tool_results.extend(
        [
            {"call_id": "read", "tool": "source_read", "result": {"content": SOURCE}},
            {
                "call_id": "locate",
                "tool": "source_locate_quotes",
                "result": {
                    "all_resolved": True,
                    "resolved": [{"quote": quote, "start_char": 0, "end_char": len(quote)}],
                    "unresolved": [],
                },
            },
            {
                "call_id": "invalid-validate",
                "tool": "schema_validate",
                "result": {"valid": False, "errors": ["draft.units[0].title is required"]},
            },
        ]
    )

    correction = adapter.next_step(context)

    assert correction.kind == "tool_calls"
    assert correction.tool_calls[0].name == "schema_validate"
    assert len(client.requests) == 1


def test_draft_evidence_must_match_source_exactly() -> None:
    draft = _draft()
    assert validate_draft_payload(draft, SOURCE) == []
    draft["units"][0]["evidence"][0]["start_char"] = 1
    errors = validate_draft_payload(draft, SOURCE)
    assert any("quote does not match" in error for error in errors)


def test_source_locator_returns_authoritative_range_and_rejects_ambiguous_quote() -> None:
    repeated_source = "重复证据。中间内容。重复证据。唯一证据。"
    context = SimpleNamespace(source=SimpleNamespace(raw_content=repeated_source))
    result = source_locate_quotes(
        context,
        {"quotes": ["重复证据。", "唯一证据。"]},
    )
    assert result["all_resolved"] is False
    assert result["resolved"] == [
        {
            "quote": "唯一证据。",
            "start_char": repeated_source.index("唯一证据。"),
            "end_char": repeated_source.index("唯一证据。") + len("唯一证据。"),
        }
    ]
    assert result["unresolved"][0]["reason"] == "ambiguous"
    assert len(result["unresolved"][0]["occurrences"]) == 2


def test_provider_503_is_retryable() -> None:
    class FailingClient:
        def post(self, _url: str, *, json: dict[str, Any]) -> FakeResponse:
            return FakeResponse({"error": "temporary"}, status_code=503)

    adapter = CLIProxyResponsesAdapter(_settings(), client=FailingClient())
    try:
        adapter.next_step(_context())
    except ModelProviderError as exc:
        assert exc.retryable is True
    else:
        raise AssertionError("HTTP 503 should be classified as retryable")


def test_short_source_uses_one_model_call_and_normalizes_evidence_ranges() -> None:
    draft = _draft()
    compact = {
        "overview": draft["agent_summary"]["overview"],
        "units": [
            {
                "title": draft["units"][0]["title"],
                "explanation": draft["units"][0]["explanation"],
                "key_points": draft["units"][0]["key_points"],
                "question": draft["units"][0]["question"],
                "answer_key": draft["units"][0]["answer_key"],
                "evidence_quote": draft["units"][0]["evidence"][0]["quote"],
            }
        ],
    }
    client = RecordingClient(
        [_response("call-quick-generate", "quick_generate", compact)]
    )
    settings = Settings(
        _env_file=None,
        model_provider="cli_proxy",
        model_base_url="http://proxy.test/v1",
        model_api_key="test-only-secret",
        model_name="gpt-5.4-mini",
        model_quick_source_max_chars=600,
    )
    adapter = CLIProxyResponsesAdapter(settings, client=client)
    context = _context()

    validation_step = adapter.next_step(context)
    normalized = validation_step.tool_calls[0].arguments["draft"]
    evidence = normalized["units"][0]["evidence"][0]
    assert validation_step.tool_calls[0].name == "schema_validate"
    assert evidence["start_char"] == 0
    assert evidence["end_char"] == len(evidence["quote"])
    context.tool_results.append(
        {
            "call_id": "call-quick-generate",
            "tool": "schema_validate",
            "result": {
                "valid": True,
                "errors": [],
                "draft_hash": canonical_draft_hash(normalized),
            },
        }
    )

    final = adapter.next_step(context)
    assert final.kind == "final"
    assert final.final_draft == normalized
    assert len(client.requests) == 1
    assert client.requests[0]["reasoning"] == {"effort": "none"}
    assert client.requests[0]["max_output_tokens"] == 2_500
    assert client.requests[0]["tools"][0]["name"] == "quick_generate"
