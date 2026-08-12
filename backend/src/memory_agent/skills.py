from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SkillDocument:
    name: str
    description: str
    version: str
    path: Path
    content: str
    keywords: tuple[str, ...]


def _frontmatter(content: str) -> dict[str, str]:
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    result: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"\'')
    return result


class SkillRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root

    def list_enabled(self) -> list[SkillDocument]:
        if not self.root.exists():
            return []
        documents: list[SkillDocument] = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            content = path.read_text(encoding="utf-8")
            meta = _frontmatter(content)
            if meta.get("enabled", "true").lower() == "false":
                continue
            name = meta.get("name", path.parent.name)
            description = meta.get("description", "")
            keyword_text = " ".join((name, description, meta.get("keywords", "")))
            keywords = tuple(
                token.lower()
                for token in re.findall(r"[A-Za-z0-9_\-\u4e00-\u9fff]+", keyword_text)
                if len(token) > 1
            )
            documents.append(
                SkillDocument(
                    name=name,
                    description=description,
                    version=meta.get("version", "1.0.0"),
                    path=path,
                    content=content,
                    keywords=keywords,
                )
            )
        return documents

    def route(self, query: str, *, limit: int = 3) -> list[SkillDocument]:
        normalized = query.lower()
        ranked: list[tuple[int, SkillDocument]] = []
        for document in self.list_enabled():
            score = sum(1 for word in document.keywords if word in normalized)
            # The general decomposition skill is the safe fallback.
            if document.name == "knowledge-decomposition":
                score += 1
            ranked.append((score, document))
        ranked.sort(key=lambda item: (-item[0], item[1].name))
        return [document for score, document in ranked if score > 0][:limit]
