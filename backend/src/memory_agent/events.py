from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_agent.models import AgentEvent, AgentRun, RunState


PUBLIC_STATE_LABELS = {
    RunState.CREATED.value: "任务已创建",
    RunState.QUEUED.value: "任务已进入队列",
    RunState.INGESTING.value: "正在读取学习材料",
    RunState.RETRIEVING_MEMORY.value: "正在检索已批准记忆",
    RunState.ROUTING_SKILLS.value: "正在选择处理技能",
    RunState.PLANNING.value: "正在制定处理计划",
    RunState.EXECUTING.value: "正在调用工具处理材料",
    RunState.DRAFTING.value: "正在生成知识草稿",
    RunState.REVIEWING.value: "正在校验草稿",
    RunState.AWAITING_USER.value: "草稿等待用户确认",
    RunState.REVISION_REQUESTED.value: "已收到修改意见",
    RunState.CONFIRMED.value: "草稿已确认",
    RunState.CURATING_MEMORY.value: "正在整理记忆候选",
    RunState.RETRY_WAIT.value: "模型服务暂时不可用，等待自动重试",
    RunState.BUDGET_EXHAUSTED.value: "已达到执行预算",
    RunState.COMPLETED.value: "任务已完成",
    RunState.FAILED.value: "任务执行失败",
    RunState.CANCELLED.value: "任务已取消",
}


def append_event(
    session: Session,
    run: AgentRun,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    visible_to_user: bool = True,
) -> AgentEvent:
    # A run has one active worker in stage one. Locking here also keeps event
    # sequence allocation safe when PostgreSQL is used.
    locked = session.scalar(select(AgentRun).where(AgentRun.id == run.id).with_for_update())
    if locked is None:
        raise LookupError(f"agent run {run.id} does not exist")
    event = AgentEvent(
        run_id=locked.id,
        seq=locked.next_event_seq,
        event_type=event_type,
        payload=payload or {},
        visible_to_user=visible_to_user,
    )
    locked.next_event_seq += 1
    session.add(event)
    session.flush()
    return event


def transition_run(
    session: Session,
    run: AgentRun,
    state: RunState,
    *,
    details: dict[str, Any] | None = None,
) -> AgentEvent:
    run.state = state.value
    now = datetime.now(UTC)
    if run.started_at is None and state not in {RunState.CREATED, RunState.QUEUED}:
        run.started_at = now
    if state in {
        RunState.BUDGET_EXHAUSTED,
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
    }:
        run.finished_at = now
    payload: dict[str, Any] = {
        "state": state.value,
        "message": PUBLIC_STATE_LABELS.get(state.value, state.value),
    }
    if details:
        payload.update(details)
    event = append_event(session, run, "run.state_changed", payload)
    session.commit()
    return event
