from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_agent.draft_contract import (
    canonical_draft_hash,
    draft_tool_parameters,
    validate_draft_payload,
)
from memory_agent.events import append_event
from memory_agent.models import AgentRun, Source, ToolInvocation, utc_now


ToolHandler = Callable[["ToolContext", dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    version: str
    description: str
    risk_level: str
    approval_mode: str
    parameters: dict[str, Any]
    handler: ToolHandler


@dataclass(slots=True)
class ToolContext:
    session: Session
    run: AgentRun
    source: Source
    user_id: UUID


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        self._definitions[definition.name] = definition

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": item.name,
                "version": item.version,
                "description": item.description,
                "risk_level": item.risk_level,
                "approval_mode": item.approval_mode,
                "parameters": item.parameters,
            }
            for item in self._definitions.values()
        ]

    def execute(
        self,
        context: ToolContext,
        *,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        definition = self._definitions.get(name)
        if definition is None:
            raise ValueError(f"unknown tool: {name}")
        stable = f"{context.run.id}:{call_id}:{name}:{arguments}"
        idempotency_key = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        existing = context.session.scalar(
            select(ToolInvocation).where(ToolInvocation.idempotency_key == idempotency_key)
        )
        if existing is not None and existing.status == "succeeded":
            return existing.result_json or {}

        invocation = existing or ToolInvocation(
            run_id=context.run.id,
            call_id=call_id,
            idempotency_key=idempotency_key,
            tool_name=name,
            tool_version=definition.version,
            risk_level=definition.risk_level,
            approval_mode=definition.approval_mode,
            arguments_json=arguments,
        )
        context.session.add(invocation)
        invocation.status = "running"
        invocation.started_at = utc_now()
        append_event(
            context.session,
            context.run,
            "tool.started",
            {"call_id": call_id, "tool": name, "risk_level": definition.risk_level},
        )
        context.session.commit()
        started = time.monotonic()
        try:
            result = definition.handler(context, arguments)
            invocation.status = "succeeded"
            invocation.result_json = result
            invocation.finished_at = utc_now()
            invocation.duration_ms = int((time.monotonic() - started) * 1000)
            context.run.tool_call_count += 1
            append_event(
                context.session,
                context.run,
                "tool.completed",
                {
                    "call_id": call_id,
                    "tool": name,
                    "duration_ms": invocation.duration_ms,
                    "result_summary": _result_summary(name, result),
                },
            )
            context.session.commit()
            return result
        except Exception as exc:
            invocation.status = "failed"
            invocation.error_code = type(exc).__name__
            invocation.error_message = str(exc)
            invocation.finished_at = utc_now()
            invocation.duration_ms = int((time.monotonic() - started) * 1000)
            append_event(
                context.session,
                context.run,
                "tool.failed",
                {"call_id": call_id, "tool": name, "error": str(exc)},
            )
            context.session.commit()
            raise


def _result_summary(name: str, result: dict[str, Any]) -> dict[str, Any]:
    if name == "source_read":
        return {"char_count": len(result.get("content", ""))}
    if name == "source_locate_quotes":
        return {
            "requested_count": result.get("requested_count", 0),
            "resolved_count": len(result.get("resolved", [])),
            "unresolved_count": len(result.get("unresolved", [])),
        }
    if name == "schema_validate":
        return {
            "valid": result.get("valid"),
            "error_count": len(result.get("errors", [])),
            "unit_count": result.get("unit_count", 0),
        }
    return {"keys": sorted(result.keys())}


def source_read(context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    start = max(0, int(arguments.get("start_char", 0)))
    end = min(len(context.source.raw_content), int(arguments.get("end_char", 50_000)))
    if end <= start:
        raise ValueError("end_char must be greater than start_char")
    return {
        "source_id": str(context.source.id),
        "title": context.source.title,
        "learning_goal": context.source.learning_goal,
        "start_char": start,
        "end_char": end,
        "content": context.source.raw_content[start:end],
    }


def source_locate_quotes(
    context: ToolContext, arguments: dict[str, Any]
) -> dict[str, Any]:
    quotes = arguments.get("quotes")
    if not isinstance(quotes, list) or not 1 <= len(quotes) <= 30:
        raise ValueError("quotes must contain between 1 and 30 exact source strings")
    if len(set(quotes)) != len(quotes):
        raise ValueError("quotes must be unique within one request")

    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    source = context.source.raw_content
    for quote in quotes:
        if not isinstance(quote, str) or not 1 <= len(quote) <= 500:
            raise ValueError("each quote must be a string between 1 and 500 characters")
        occurrences: list[dict[str, int]] = []
        cursor = 0
        while len(occurrences) < 20:
            start = source.find(quote, cursor)
            if start < 0:
                break
            occurrences.append({"start_char": start, "end_char": start + len(quote)})
            cursor = start + 1
        if len(occurrences) == 1:
            resolved.append({"quote": quote, **occurrences[0]})
            continue
        unresolved.append(
            {
                "quote": quote,
                "reason": "not_found" if not occurrences else "ambiguous",
                "occurrences": occurrences,
                "hint": (
                    "copy the quote verbatim from source_read"
                    if not occurrences
                    else "use a longer surrounding quote that occurs only once"
                ),
            }
        )
    return {
        "requested_count": len(quotes),
        "all_resolved": not unresolved,
        "resolved": resolved,
        "unresolved": unresolved,
    }


def schema_validate(_context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    draft = arguments.get("draft")
    errors = validate_draft_payload(draft, _context.source.raw_content)
    result: dict[str, Any] = {"valid": not errors, "errors": errors}
    if not errors:
        result["draft_hash"] = canonical_draft_hash(draft)
        result["unit_count"] = len(draft.get("units", []))
    return result


def build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="source_read",
            version="1.0.0",
            description="Read a bounded character range from the user-provided source.",
            risk_level="low",
            approval_mode="automatic",
            parameters={
                "type": "object",
                "properties": {
                    "start_char": {"type": "integer", "minimum": 0},
                    "end_char": {"type": "integer", "minimum": 1, "maximum": 50_000},
                },
                "required": ["start_char", "end_char"],
                "additionalProperties": False,
            },
            handler=source_read,
        )
    )
    registry.register(
        ToolDefinition(
            name="source_locate_quotes",
            version="1.0.0",
            description=(
                "Locate exact, preferably unique source quotes and return authoritative "
                "zero-based end-exclusive character ranges before draft validation."
            ),
            risk_level="low",
            approval_mode="automatic",
            parameters={
                "type": "object",
                "properties": {
                    "quotes": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 500},
                        "minItems": 1,
                        "maxItems": 30,
                    }
                },
                "required": ["quotes"],
                "additionalProperties": False,
            },
            handler=source_locate_quotes,
        )
    )
    registry.register(
        ToolDefinition(
            name="schema_validate",
            version="1.0.0",
            description="Validate the structured knowledge draft before persistence.",
            risk_level="low",
            approval_mode="automatic",
            parameters=draft_tool_parameters(),
            handler=schema_validate,
        )
    )
    return registry
