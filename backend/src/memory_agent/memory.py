from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_agent.models import MemoryItem


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    id: UUID
    kind: str
    content: str
    compact_summary: str
    score: float


def _terms(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_\-]+|[\u4e00-\u9fff]{2,}", text)
        if len(token) > 1
    }


def retrieve_memories(
    session: Session,
    *,
    user_id: UUID,
    profile_id: UUID,
    query: str,
    limit: int = 8,
) -> list[RetrievedMemory]:
    candidates = session.scalars(
        select(MemoryItem).where(
            MemoryItem.user_id == user_id,
            MemoryItem.profile_id == profile_id,
            MemoryItem.status == "active",
        )
    ).all()
    query_terms = _terms(query)
    ranked: list[tuple[float, MemoryItem]] = []
    for item in candidates:
        document_terms = _terms(f"{item.canonical_key} {item.compact_summary} {item.content}")
        overlap = len(query_terms & document_terms) / max(1, len(query_terms))
        score = overlap * 0.65 + item.importance * 0.2 + item.confidence * 0.15
        if overlap > 0:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: (-pair[0], pair[1].created_at))
    selected = ranked[:limit]
    now = datetime.now(UTC)
    for _, item in selected:
        item.last_accessed_at = now
    session.flush()
    return [
        RetrievedMemory(
            id=item.id,
            kind=item.kind,
            content=item.content,
            compact_summary=item.compact_summary,
            score=round(score, 4),
        )
        for score, item in selected
    ]
