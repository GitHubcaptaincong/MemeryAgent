from __future__ import annotations

from functools import lru_cache

from memory_agent.agent import AgentRuntime
from memory_agent.config import get_settings
from memory_agent.database import SessionLocal
from memory_agent.jobs import JobRunner
from memory_agent.model_adapters import build_model_adapter
from memory_agent.skills import SkillRegistry
from memory_agent.tools import build_tool_registry


@lru_cache
def get_agent_runtime() -> AgentRuntime:
    settings = get_settings()
    return AgentRuntime(
        session_factory=SessionLocal,
        settings=settings,
        model=build_model_adapter(settings),
        tools=build_tool_registry(),
        skills=SkillRegistry(settings.skill_root),
    )


@lru_cache
def get_job_runner() -> JobRunner:
    return JobRunner(
        session_factory=SessionLocal,
        runtime=get_agent_runtime(),
        settings=get_settings(),
    )
