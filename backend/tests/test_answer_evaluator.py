from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from memory_agent.answer_evaluator import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    AnswerEvaluationInput,
    CLIProxyAnswerEvaluator,
    FakeAnswerEvaluator,
    build_answer_evaluator,
)
from memory_agent.model_adapters import ModelProtocolError, ModelProviderError


class FakeResponse:
    def __init__(self, payload: Any, *, status_code: int = 200, text: str | None = None) -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self) -> Any:
        return self.payload


class RecordingClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, *, json: dict[str, Any]) -> FakeResponse:
        self.requests.append((url, json))
        return self.responses.pop(0)


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "model_provider": "cli_proxy",
        "model_base_url": "http://proxy.test/v1",
        "model_api_key_value": "test-only-secret",
        "model_name": "gpt-5.4-mini",
        "model_verify_ssl": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _input() -> AnswerEvaluationInput:
    return AnswerEvaluationInput(
        question="主动回忆为什么能发现知识缺口？",
        answer_key=["主动解释", "发现知识缺口"],
        answer="主动解释会暴露卡顿，从而发现知识缺口。",
    )


def _arguments() -> dict[str, Any]:
    return {
        "suggested_rating": 4,
        "summary": "回答覆盖了两个核心要点。",
        "covered_points": [
            {"point_index": 0, "evidence": "主动解释"},
            {"point_index": 1, "evidence": "发现知识缺口"},
        ],
        "missing_points": [],
        "confidence": 0.92,
    }


def _provider_response(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "resp-evaluation-1",
        "output": [
            {"type": "reasoning", "encrypted_content": "opaque"},
            {
                "type": "function_call",
                "call_id": "call-evaluation-1",
                "name": "submit_answer_evaluation",
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        ],
        "usage": {
            "input_tokens": 80,
            "output_tokens": 35,
            "output_tokens_details": {"reasoning_tokens": 7},
        },
    }


def test_fake_evaluator_deterministically_classifies_keyword_coverage() -> None:
    evaluator = FakeAnswerEvaluator()
    full = evaluator.evaluate(_input())
    partial = evaluator.evaluate(
        AnswerEvaluationInput(
            question="主动回忆为什么能发现知识缺口？",
            answer_key=["主动解释", "发现知识缺口"],
            answer="我会先主动解释。",
        )
    )

    assert full.status == "completed"
    assert full.suggested_rating == 4
    assert [item["point_index"] for item in full.covered_points] == [0, 1]
    assert full.missing_points == []
    assert partial.suggested_rating == 2
    assert partial.covered_points[0]["point"] == "主动解释"
    assert partial.missing_points[0]["point"] == "发现知识缺口"


def test_build_answer_evaluator_uses_configured_provider() -> None:
    assert isinstance(
        build_answer_evaluator(SimpleNamespace(model_provider="fake")),
        FakeAnswerEvaluator,
    )
    with pytest.raises(ValueError, match="not implemented"):
        build_answer_evaluator(SimpleNamespace(model_provider="unknown"))


def test_cli_proxy_evaluator_uses_one_strict_responses_tool_and_records_usage() -> None:
    client = RecordingClient([FakeResponse(_provider_response(_arguments()))])
    evaluator = CLIProxyAnswerEvaluator(_settings(), client=client)

    result = evaluator.evaluate(_input())

    assert result.status == "completed"
    assert result.suggested_rating == 4
    assert result.provider == "cli_proxy"
    assert result.model_name == "gpt-5.4-mini"
    assert result.input_tokens == 80
    assert result.output_tokens == 35
    assert result.reasoning_tokens == 7
    assert result.provider_response_id == "resp-evaluation-1"
    assert result.covered_points[0]["point"] == "主动解释"
    assert result.to_payload()["suggested_rating"] == 4

    url, payload = client.requests[0]
    assert url == "http://proxy.test/v1/responses"
    assert payload["store"] is False
    assert payload["tool_choice"] == "required"
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["max_output_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS
    assert len(payload["tools"]) == 1
    assert payload["tools"][0]["name"] == "submit_answer_evaluation"
    assert payload["tools"][0]["strict"] is True
    assert payload["tools"][0]["parameters"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda value: value["covered_points"].append(
                {"point_index": 2, "evidence": "越界"}
            ),
            "out of range",
        ),
        (
            lambda value: value["missing_points"].append(
                {"point_index": 0, "suggestion": "重复分类"}
            ),
            "must be unique",
        ),
        (
            lambda value: value["covered_points"].pop(),
            "classify every",
        ),
    ],
)
def test_cli_proxy_evaluator_rejects_invalid_point_classification(
    mutate: Any, message: str
) -> None:
    arguments = _arguments()
    mutate(arguments)
    client = RecordingClient([FakeResponse(_provider_response(arguments))])
    evaluator = CLIProxyAnswerEvaluator(_settings(), client=client)

    with pytest.raises(ModelProtocolError, match=message):
        evaluator.evaluate(_input())


def test_cli_proxy_evaluator_classifies_http_failures_and_redacts_key() -> None:
    client = RecordingClient(
        [
            FakeResponse(
                {},
                status_code=429,
                text="provider rejected test-only-secret",
            )
        ]
    )
    evaluator = CLIProxyAnswerEvaluator(_settings(), client=client)

    with pytest.raises(ModelProviderError) as error:
        evaluator.evaluate(_input())

    assert error.value.retryable is True
    assert "test-only-secret" not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_answer_evaluation_input_rejects_empty_fields() -> None:
    with pytest.raises(ValueError, match="answer must not be empty"):
        AnswerEvaluationInput(question="问题", answer_key=["要点"], answer="   ")
