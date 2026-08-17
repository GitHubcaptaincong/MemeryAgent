from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory_agent.config import Settings
from memory_agent.events import append_event, transition_run
from memory_agent.memory import retrieve_memories
from memory_agent.model_adapters import AgentModelContext, ModelAdapter, ModelProviderError
from memory_agent.models import (
    AgentProfile,
    AgentCheckpoint,
    AgentEvent,
    AgentRun,
    DraftSourceSpan,
    DraftUnit,
    KnowledgeDraft,
    RunState,
    Source,
    TERMINAL_RUN_STATES,
)
from memory_agent.skills import SkillRegistry
from memory_agent.tools import ToolContext, ToolRegistry


class AgentRuntime:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        settings: Settings,
        model: ModelAdapter,
        tools: ToolRegistry,
        skills: SkillRegistry,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.model = model
        self.tools = tools
        self.skills = skills

    def execute(self, run_id: UUID) -> None:
        session = self.session_factory()
        try:
            run = session.get(AgentRun, run_id)
            if run is None:
                raise LookupError(f"agent run {run_id} not found")
            if run.state in TERMINAL_RUN_STATES or run.state == RunState.AWAITING_USER.value:
                return
            source = session.get(Source, run.source_id)
            if source is None or source.user_id != run.user_id:
                raise LookupError("source does not exist for this user")
            profile = session.get(AgentProfile, run.profile_id)
            if profile is None:
                raise LookupError("agent profile does not exist")

            if run.state not in {RunState.CREATED.value, RunState.QUEUED.value}:
                previous_state = run.state
                run.error_code = None
                run.error_message = None
                run.stop_reason = None
                run.provider_state = {
                    "model": self.model.model_name,
                    "store": False,
                    "recovery_strategy": "fresh_context_replay",
                    "previous_state": previous_state,
                }
                append_event(
                    session,
                    run,
                    "run.recovery_started",
                    {
                        "previous_state": previous_state,
                        "strategy": "fresh_context_replay",
                        "provider_reasoning_restored": False,
                    },
                )
                session.commit()

            self._check_cancelled(session, run)
            transition_run(session, run, RunState.INGESTING)
            append_event(
                session,
                run,
                "source.loaded",
                {
                    "source_id": str(source.id),
                    "title": source.title,
                    "char_count": source.char_count,
                    "content_type": source.content_type,
                },
            )
            session.commit()

            self._check_cancelled(session, run)
            transition_run(session, run, RunState.RETRIEVING_MEMORY)
            memories = retrieve_memories(
                session,
                user_id=run.user_id,
                profile_id=run.profile_id,
                query=f"{source.learning_goal}\n{source.title}\n{source.raw_content[:1500]}",
            )
            append_event(
                session,
                run,
                "memory.retrieved",
                {
                    "count": len(memories),
                    "items": [
                        {"id": str(item.id), "kind": item.kind, "score": item.score}
                        for item in memories
                    ],
                    "policy": "approved_active_only",
                },
            )
            session.commit()

            self._check_cancelled(session, run)
            transition_run(session, run, RunState.ROUTING_SKILLS)
            selected_skills = self.skills.route(
                f"{source.learning_goal}\n{source.title}\n{source.raw_content[:1000]}"
            )
            append_event(
                session,
                run,
                "skills.selected",
                {
                    "skills": [
                        {"name": item.name, "version": item.version, "description": item.description}
                        for item in selected_skills
                    ],
                    "mutable_by_agent": False,
                },
            )
            session.commit()

            transition_run(session, run, RunState.PLANNING)
            append_event(
                session,
                run,
                "agent.plan_created",
                {
                    "objective": source.learning_goal,
                    "processing_mode": (
                        "quick"
                        if (
                            self.settings.model_provider == "cli_proxy"
                            and 0
                            < source.char_count
                            <= self.settings.model_quick_source_max_chars
                        )
                        else "full_agent"
                    ),
                    "first_feedback_target_ms": 4_000,
                    "steps": [
                        "读取用户提供的材料",
                        "拆分 1-10 个开放问答知识单元",
                        "由工具定位逐字证据并返回精确字符区间",
                        "校验结构、证据位置和执行预算",
                        "保存为等待用户确认的草稿",
                    ],
                },
            )
            session.commit()

            context = AgentModelContext(
                source_title=source.title,
                learning_goal=source.learning_goal,
                source_char_count=source.char_count,
                profile_summary=profile.core_profile_summary,
                retrieved_memories=[
                    {
                        "id": str(item.id),
                        "kind": item.kind,
                        "summary": item.compact_summary,
                        "score": item.score,
                    }
                    for item in memories
                ],
                selected_skills=[
                    {
                        "name": item.name,
                        "version": item.version,
                        "description": item.description,
                        "content": item.content,
                    }
                    for item in selected_skills
                ],
                tools=self.tools.definitions(),
                source_content=source.raw_content,
            )
            transition_run(session, run, RunState.EXECUTING)
            self._agent_loop(session, run, source, context)
        except Exception as exc:
            session.rollback()
            run = session.get(AgentRun, run_id)
            if run is not None and run.state == RunState.CANCELLED.value:
                return
            if run is not None and run.state not in TERMINAL_RUN_STATES:
                run.error_code = type(exc).__name__
                run.error_message = str(exc)
                if isinstance(exc, ModelProviderError) and exc.retryable:
                    run.stop_reason = "transient_model_provider_error"
                    run.provider_state = {
                        "model": self.model.model_name,
                        "store": False,
                        "recovery_strategy": "fresh_context_replay",
                        "provider_reasoning_persisted": False,
                    }
                    append_event(
                        session,
                        run,
                        "run.retryable_error",
                        {
                            "error_code": type(exc).__name__,
                            "message": str(exc),
                            "strategy": "fresh_context_replay",
                        },
                    )
                    transition_run(session, run, RunState.RETRY_WAIT)
                else:
                    append_event(
                        session,
                        run,
                        "run.failed",
                        {"error_code": type(exc).__name__, "message": str(exc)},
                    )
                    transition_run(session, run, RunState.FAILED)
            raise
        finally:
            session.close()

    def _agent_loop(
        self,
        session: Session,
        run: AgentRun,
        source: Source,
        context: AgentModelContext,
    ) -> None:
        loop_steps = 0
        while loop_steps <= self.settings.agent_max_tool_calls:
            self._check_cancelled(session, run)
            step = self.model.next_step(context)
            loop_steps += 1
            if step.usage is not None:
                run.input_tokens += step.usage.input_tokens
                run.output_tokens += step.usage.output_tokens
            if step.provider_response_id:
                run.provider_state = {
                    "last_response_id": step.provider_response_id,
                    "model": self.model.model_name,
                    "store": False,
                    "replay_item_count": len(context.scratch.get("provider_items", [])),
                }
            append_event(
                session,
                run,
                "agent.decision",
                {
                    "decision_type": step.kind,
                    "summary": step.summary,
                    "tool_names": [call.name for call in step.tool_calls],
                    "usage": (
                        {
                            "input_tokens": step.usage.input_tokens,
                            "output_tokens": step.usage.output_tokens,
                            "reasoning_tokens": step.usage.reasoning_tokens,
                        }
                        if step.usage
                        else None
                    ),
                },
            )
            session.commit()
            if step.kind == "final":
                if step.final_draft is None:
                    raise RuntimeError("model returned final without a draft")
                transition_run(session, run, RunState.DRAFTING)
                draft = self._persist_draft(session, run, source, step.final_draft)
                append_event(
                    session,
                    run,
                    "draft.created",
                    {
                        "draft_id": str(draft.id),
                        "version": draft.version,
                        "unit_count": len(draft.units),
                    },
                )
                run.stop_reason = "awaiting_user_confirmation"
                session.commit()
                self._create_checkpoint(
                    session,
                    run=run,
                    source=source,
                    draft=draft,
                    context=context,
                )
                transition_run(
                    session,
                    run,
                    RunState.AWAITING_USER,
                    details={"draft_id": str(draft.id)},
                )
                return

            if not step.tool_calls:
                raise RuntimeError("tool_calls decision did not contain any calls")
            for call in step.tool_calls:
                if run.tool_call_count >= self.settings.agent_max_tool_calls:
                    run.stop_reason = "tool_call_budget_exhausted"
                    transition_run(session, run, RunState.BUDGET_EXHAUSTED)
                    return
                if call.name == "schema_validate":
                    transition_run(session, run, RunState.REVIEWING)
                result = self.tools.execute(
                    ToolContext(session=session, run=run, source=source, user_id=run.user_id),
                    call_id=call.call_id,
                    name=call.name,
                    arguments=call.arguments,
                )
                context.tool_results.append(
                    {"call_id": call.call_id, "tool": call.name, "result": result}
                )
                if call.name == "source_read":
                    transition_run(session, run, RunState.DRAFTING)
        run.stop_reason = "agent_loop_budget_exhausted"
        transition_run(session, run, RunState.BUDGET_EXHAUSTED)

    @staticmethod
    def _persist_draft(
        session: Session,
        run: AgentRun,
        source: Source,
        payload: dict[str, Any],
    ) -> KnowledgeDraft:
        current_version = session.scalar(
            select(func.max(KnowledgeDraft.version)).where(
                KnowledgeDraft.source_id == source.id,
                KnowledgeDraft.user_id == run.user_id,
            )
        )
        draft = KnowledgeDraft(
            user_id=run.user_id,
            source_id=source.id,
            run_id=run.id,
            version=(current_version or 0) + 1,
            status="draft",
            learning_goal=payload.get("learning_goal", source.learning_goal),
            agent_summary=payload.get("agent_summary", {}),
        )
        session.add(draft)
        session.flush()
        for index, unit_payload in enumerate(payload["units"], start=1):
            unit = DraftUnit(
                draft_id=draft.id,
                position=unit_payload.get("position", index),
                title=unit_payload["title"],
                learning_objective=unit_payload["learning_objective"],
                explanation=unit_payload["explanation"],
                key_points=unit_payload.get("key_points", []),
                question=unit_payload["question"],
                answer_key=unit_payload["answer_key"],
                hints=unit_payload.get("hints", []),
                tags=unit_payload.get("tags", []),
                applicable_scenarios=unit_payload.get("applicable_scenarios", []),
                confidence=unit_payload.get("confidence", 0.0),
                requires_user_confirmation=unit_payload.get(
                    "requires_user_confirmation", True
                ),
                uncertainties=unit_payload.get("uncertainties", []),
            )
            session.add(unit)
            session.flush()
            for evidence in unit_payload.get("evidence", []):
                session.add(
                    DraftSourceSpan(
                        unit_id=unit.id,
                        evidence_type=evidence.get("evidence_type", "source_span"),
                        source_id=source.id,
                        start_char=evidence.get("start_char"),
                        end_char=evidence.get("end_char"),
                        quote=evidence.get("quote"),
                        url=evidence.get("url") or source.origin_url,
                        retrieved_at=source.retrieved_at,
                    )
                )
        session.flush()
        return draft

    def _create_checkpoint(
        self,
        session: Session,
        *,
        run: AgentRun,
        source: Source,
        draft: KnowledgeDraft,
        context: AgentModelContext,
    ) -> AgentCheckpoint:
        events = session.scalars(
            select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.seq)
        ).all()
        if not events:
            raise RuntimeError("cannot checkpoint a run without events")
        serialized = json.dumps(
            [
                {"seq": event.seq, "type": event.event_type, "payload": event.payload}
                for event in events
            ],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        previous = session.scalar(
            select(AgentCheckpoint)
            .where(AgentCheckpoint.run_id == run.id)
            .order_by(AgentCheckpoint.checkpoint_version.desc())
            .limit(1)
        )
        summary = {
            "objective": source.learning_goal,
            "source": {"id": str(source.id), "title": source.title},
            "selected_skills": [item["name"] for item in context.selected_skills],
            "tool_results": [
                {
                    "call_id": item["call_id"],
                    "tool": item["tool"],
                    "result_keys": sorted(item["result"].keys()),
                }
                for item in context.tool_results
            ],
            "output": {"draft_id": str(draft.id), "unit_count": len(draft.units)},
            "pending": ["user_draft_confirmation", "separate_memory_approval"],
        }
        compact = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        checkpoint = AgentCheckpoint(
            run_id=run.id,
            checkpoint_version=(previous.checkpoint_version + 1) if previous else 1,
            previous_checkpoint_id=previous.id if previous else None,
            covered_event_seq_start=events[0].seq,
            covered_event_seq_end=events[-1].seq,
            summary_json=summary,
            raw_events_digest=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            prompt_version="stage2-cli-proxy-v2",
            model=self.model.model_name,
            tokens_before=max(1, len(serialized) // 4),
            tokens_after=max(1, len(compact) // 4),
        )
        session.add(checkpoint)
        session.flush()
        append_event(
            session,
            run,
            "checkpoint.created",
            {
                "checkpoint_id": str(checkpoint.id),
                "version": checkpoint.checkpoint_version,
                "covered_event_seq_end": checkpoint.covered_event_seq_end,
                "tokens_before": checkpoint.tokens_before,
                "tokens_after": checkpoint.tokens_after,
                "raw_events_preserved": True,
            },
        )
        session.commit()
        return checkpoint

    @staticmethod
    def _check_cancelled(session: Session, run: AgentRun) -> None:
        session.refresh(run, attribute_names=["cancel_requested"])
        if not run.cancel_requested:
            return
        run.stop_reason = "cancel_requested"
        transition_run(session, run, RunState.CANCELLED)
        raise RuntimeError("run was cancelled")
