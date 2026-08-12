from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "evidence_type": {"type": "string", "enum": ["source_span"]},
        "start_char": {"type": "integer", "minimum": 0},
        "end_char": {"type": "integer", "minimum": 1},
        "quote": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": ["evidence_type", "start_char", "end_char", "quote"],
    "additionalProperties": False,
}

UNIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "position": {"type": "integer", "minimum": 1, "maximum": 10},
        "title": {"type": "string", "minLength": 1, "maxLength": 300},
        "learning_objective": {"type": "string", "minLength": 1, "maxLength": 800},
        "explanation": {"type": "string", "minLength": 1, "maxLength": 4000},
        "key_points": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
            "minItems": 1,
            "maxItems": 6,
        },
        "question": {"type": "string", "minLength": 1, "maxLength": 1000},
        "answer_key": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
            "minItems": 1,
            "maxItems": 8,
        },
        "hints": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
            "maxItems": 4,
        },
        "tags": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 100},
            "maxItems": 8,
        },
        "applicable_scenarios": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
            "maxItems": 5,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "requires_user_confirmation": {"type": "boolean"},
        "uncertainties": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
            "maxItems": 5,
        },
        "evidence": {
            "type": "array",
            "items": EVIDENCE_SCHEMA,
            "minItems": 1,
            "maxItems": 3,
        },
    },
    "required": [
        "position",
        "title",
        "learning_objective",
        "explanation",
        "key_points",
        "question",
        "answer_key",
        "hints",
        "tags",
        "applicable_scenarios",
        "confidence",
        "requires_user_confirmation",
        "uncertainties",
        "evidence",
    ],
    "additionalProperties": False,
}

DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "learning_goal": {"type": "string", "minLength": 1, "maxLength": 500},
        "agent_summary": {
            "type": "object",
            "properties": {
                "overview": {"type": "string", "minLength": 1, "maxLength": 1000},
                "generation_mode": {"type": "string", "enum": ["cli_proxy"]},
                "requires_user_confirmation": {"type": "boolean"},
            },
            "required": ["overview", "generation_mode", "requires_user_confirmation"],
            "additionalProperties": False,
        },
        "units": {
            "type": "array",
            "items": UNIT_SCHEMA,
            "minItems": 1,
            "maxItems": 10,
        },
    },
    "required": ["learning_goal", "agent_summary", "units"],
    "additionalProperties": False,
}

QUICK_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overview": {"type": "string", "minLength": 1, "maxLength": 500},
        "units": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 120},
                    "explanation": {"type": "string", "minLength": 1, "maxLength": 800},
                    "key_points": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 240},
                        "minItems": 1,
                        "maxItems": 4,
                    },
                    "question": {"type": "string", "minLength": 1, "maxLength": 300},
                    "answer_key": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 240},
                        "minItems": 1,
                        "maxItems": 4,
                    },
                    "evidence_quote": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                    },
                },
                "required": [
                    "title",
                    "explanation",
                    "key_points",
                    "question",
                    "answer_key",
                    "evidence_quote",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overview", "units"],
    "additionalProperties": False,
}


def draft_tool_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"draft": copy.deepcopy(DRAFT_SCHEMA)},
        "required": ["draft"],
        "additionalProperties": False,
    }


def quick_draft_tool_parameters() -> dict[str, Any]:
    return copy.deepcopy(QUICK_DRAFT_SCHEMA)


def canonical_draft_hash(draft: dict[str, Any]) -> str:
    serialized = json.dumps(draft, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def normalize_evidence_ranges(draft: Any, source_content: str) -> Any:
    """Fill authoritative ranges when a verbatim quote occurs exactly once."""
    normalized = copy.deepcopy(draft)
    if not isinstance(normalized, dict):
        return normalized
    units = normalized.get("units")
    if not isinstance(units, list):
        return normalized
    for unit in units:
        if not isinstance(unit, dict) or not isinstance(unit.get("evidence"), list):
            continue
        for evidence in unit["evidence"]:
            if not isinstance(evidence, dict):
                continue
            quote = evidence.get("quote")
            if not isinstance(quote, str) or not quote:
                continue
            first = source_content.find(quote)
            if first < 0:
                continue
            second = source_content.find(quote, first + 1)
            if second >= 0:
                start = evidence.get("start_char")
                end = evidence.get("end_char")
                if (
                    isinstance(start, int)
                    and isinstance(end, int)
                    and source_content[start:end] == quote
                ):
                    continue
                continue
            evidence["start_char"] = first
            evidence["end_char"] = first + len(quote)
    return normalized


def validate_draft_payload(draft: Any, source_content: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(draft, dict):
        return ["draft must be an object"]
    for key in ("learning_goal", "agent_summary", "units"):
        if key not in draft:
            errors.append(f"draft missing: {key}")
    units = draft.get("units")
    if not isinstance(units, list) or not 1 <= len(units) <= 10:
        errors.append("draft.units must contain between 1 and 10 items")
        return errors

    required = {
        "position",
        "title",
        "learning_objective",
        "explanation",
        "key_points",
        "question",
        "answer_key",
        "hints",
        "tags",
        "applicable_scenarios",
        "confidence",
        "requires_user_confirmation",
        "uncertainties",
        "evidence",
    }
    positions: list[int] = []
    for index, unit in enumerate(units):
        prefix = f"units[{index}]"
        if not isinstance(unit, dict):
            errors.append(f"{prefix} must be an object")
            continue
        missing = sorted(key for key in required if key not in unit)
        if missing:
            errors.append(f"{prefix} missing: {', '.join(missing)}")
            continue
        position = unit.get("position")
        if not isinstance(position, int):
            errors.append(f"{prefix}.position must be an integer")
        else:
            positions.append(position)
        for key in ("title", "learning_objective", "explanation", "question"):
            if not isinstance(unit.get(key), str) or not unit[key].strip():
                errors.append(f"{prefix}.{key} must be a non-empty string")
        for key in (
            "key_points",
            "answer_key",
            "hints",
            "tags",
            "applicable_scenarios",
            "uncertainties",
        ):
            value = unit.get(key)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                errors.append(f"{prefix}.{key} must be a string array")
        confidence = unit.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append(f"{prefix}.confidence must be between 0 and 1")
        evidence_items = unit.get("evidence")
        if not isinstance(evidence_items, list) or not evidence_items:
            errors.append(f"{prefix}.evidence must contain at least one source span")
            continue
        for evidence_index, evidence in enumerate(evidence_items):
            evidence_prefix = f"{prefix}.evidence[{evidence_index}]"
            if not isinstance(evidence, dict):
                errors.append(f"{evidence_prefix} must be an object")
                continue
            start = evidence.get("start_char")
            end = evidence.get("end_char")
            quote = evidence.get("quote")
            if not isinstance(start, int) or not isinstance(end, int):
                errors.append(f"{evidence_prefix} start_char/end_char must be integers")
                continue
            if start < 0 or end <= start or end > len(source_content):
                errors.append(
                    f"{evidence_prefix} range [{start}, {end}) is outside source length {len(source_content)}"
                )
                continue
            expected = source_content[start:end]
            if quote != expected:
                suggestion = source_content.find(str(quote)) if quote else -1
                hint = (
                    f"; exact quote occurs at [{suggestion}, {suggestion + len(str(quote))})"
                    if suggestion >= 0
                    else "; quote was not found verbatim in source"
                )
                errors.append(f"{evidence_prefix}.quote does not match the source range{hint}")

    if sorted(positions) != list(range(1, len(units) + 1)):
        errors.append("unit positions must be unique and consecutive starting at 1")
    return errors[:30]
