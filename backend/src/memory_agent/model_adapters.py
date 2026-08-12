from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from memory_agent.config import Settings


class ModelProviderError(RuntimeError):
    """Provider failure with an explicit retry classification."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ModelProtocolError(RuntimeError):
    """A successful provider response that violates the local agent contract."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelStep:
    kind: Literal["tool_calls", "final"]
    summary: str
    tool_calls: tuple[ToolCall, ...] = ()
    final_draft: dict[str, Any] | None = None
    usage: ModelUsage | None = None
    provider_response_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(slots=True)
class AgentModelContext:
    source_title: str
    learning_goal: str
    source_char_count: int
    profile_summary: str
    retrieved_memories: list[dict[str, Any]]
    selected_skills: list[dict[str, str]]
    tools: list[dict[str, str]]
    source_content: str = ""
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    scratch: dict[str, Any] = field(default_factory=dict)


class ModelAdapter(Protocol):
    provider: str
    model_name: str

    def next_step(self, context: AgentModelContext) -> ModelStep: ...


def _paragraphs(content: str) -> list[tuple[int, int, str]]:
    results: list[tuple[int, int, str]] = []
    for match in re.finditer(r"[^\n]+", content):
        text = match.group(0).strip()
        if len(text) < 12:
            continue
        start = content.find(text, match.start(), match.end())
        results.append((start, start + len(text), text))
    if results:
        return results
    stripped = content.strip()
    start = content.find(stripped)
    return [(start, start + len(stripped), stripped)] if stripped else []


def _title(text: str, position: int) -> str:
    first = re.split(r"[。！？.!?；;：:]", text, maxsplit=1)[0].strip("#* -\t")
    if not first:
        return f"知识单元 {position}"
    return first[:36]


def _key_points(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"[。！？.!?；;]", text) if part.strip()]
    return [part[:120] for part in parts[:4]] or [text[:120]]


def build_fake_draft(content: str, *, title: str, learning_goal: str) -> dict[str, Any]:
    paragraphs = _paragraphs(content)
    if not paragraphs:
        raise ValueError("source content is empty")
    selected = paragraphs[: min(5, max(1, len(paragraphs)))]
    units: list[dict[str, Any]] = []
    for index, (start, end, paragraph) in enumerate(selected, start=1):
        points = _key_points(paragraph)
        unit_title = _title(paragraph, index)
        evidence_quote = paragraph[:500]
        units.append(
            {
                "position": index,
                "title": unit_title,
                "learning_objective": f"理解“{unit_title}”并能用自己的话解释。",
                "explanation": paragraph,
                "key_points": points,
                "question": f"请结合材料说明：{unit_title}的核心含义是什么？",
                "answer_key": points,
                "hints": ["先定位材料中的关键概念", "再说明概念之间的关系"],
                "tags": [title[:30], "阶段一草稿"],
                "applicable_scenarios": [learning_goal],
                "confidence": 0.78,
                "requires_user_confirmation": True,
                "uncertainties": ["该内容由 Fake Model 生成，接入真实模型后需重新评估表达质量。"],
                "evidence": [
                    {
                        "evidence_type": "source_span",
                        "start_char": start,
                        "end_char": start + len(evidence_quote),
                        "quote": evidence_quote,
                    }
                ],
            }
        )
    return {
        "learning_goal": learning_goal,
        "agent_summary": {
            "overview": f"从《{title}》中拆分出 {len(units)} 个开放问答知识单元。",
            "generation_mode": "fake",
            "requires_user_confirmation": True,
        },
        "units": units,
    }


class FakeModelAdapter:
    """Deterministic adapter that exercises the same bounded agent loop as a real LLM."""

    provider = "fake"
    model_name = "fake-deterministic-v1"

    def next_step(self, context: AgentModelContext) -> ModelStep:
        phase = int(context.scratch.get("phase", 0))
        if phase == 0:
            context.scratch["phase"] = 1
            return ModelStep(
                kind="tool_calls",
                summary="先读取用户授权的完整材料，再决定如何拆分知识单元。",
                tool_calls=(
                    ToolCall(
                        call_id="read-source-1",
                        name="source_read",
                        arguments={"start_char": 0, "end_char": context.source_char_count},
                    ),
                ),
            )
        if phase == 1:
            source_result = next(
                (item["result"] for item in context.tool_results if item["tool"] == "source_read"),
                None,
            )
            if not source_result:
                raise RuntimeError("source_read result is required before drafting")
            draft = build_fake_draft(
                source_result["content"],
                title=context.source_title,
                learning_goal=context.learning_goal,
            )
            context.scratch["draft"] = draft
            context.scratch["phase"] = 2
            return ModelStep(
                kind="tool_calls",
                summary="已形成结构化草稿，调用校验工具检查单元数量和必填字段。",
                tool_calls=(
                    ToolCall(
                        call_id="validate-draft-1",
                        name="schema_validate",
                        arguments={"draft": draft},
                    ),
                ),
            )
        validation = next(
            (
                item["result"]
                for item in reversed(context.tool_results)
                if item["tool"] == "schema_validate"
            ),
            None,
        )
        if not validation or not validation.get("valid"):
            raise RuntimeError(f"draft validation failed: {validation}")
        return ModelStep(
            kind="final",
            summary="草稿结构校验通过，交由用户审阅和确认。",
            final_draft=context.scratch["draft"],
        )


def build_model_adapter(settings: Settings) -> ModelAdapter:
    if settings.model_provider == "fake":
        return FakeModelAdapter()
    if settings.model_provider == "cli_proxy":
        from memory_agent.cli_proxy_adapter import CLIProxyResponsesAdapter

        return CLIProxyResponsesAdapter(settings)
    raise ValueError(
        f"model provider '{settings.model_provider}' is not implemented; "
        "use APP_MODEL_PROVIDER=fake or cli_proxy"
    )
