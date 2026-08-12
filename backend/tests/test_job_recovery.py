from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from memory_agent.bootstrap import ensure_local_identity
from memory_agent.config import Settings
from memory_agent.database import SessionLocal
from memory_agent.jobs import JobRunner
from memory_agent.model_adapters import ModelProviderError
from memory_agent.models import AgentEvent, AgentRun, BackgroundJob, RunState
from memory_agent.schemas import SourceCreate
from memory_agent.services import create_run, create_source


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        model_provider="fake",
        inline_worker=False,
        agent_max_seconds=30,
    )


def _new_job(key: str) -> tuple[AgentRun, BackgroundJob]:
    settings = _settings()
    session = SessionLocal()
    try:
        user, profile = ensure_local_identity(session, settings)
        source = create_source(
            session,
            user_id=user.id,
            data=SourceCreate(
                title="后台恢复测试",
                learning_goal="验证任务可以从瞬时故障或过期租约恢复",
                content="后台任务使用租约避免重复执行。租约过期后，新的工作进程可以安全接管。",
            ),
        )
        run, job, _ = create_run(
            session,
            user_id=user.id,
            profile=profile,
            source_id=source.id,
            idempotency_key=key,
            settings=settings,
        )
        return run, job
    finally:
        session.close()


def test_retryable_provider_error_requeues_job() -> None:
    run, job = _new_job("retryable-provider-error")

    class RetryWaitRuntime:
        def execute(self, run_id):
            session = SessionLocal()
            try:
                current = session.get(AgentRun, run_id)
                assert current is not None
                current.state = RunState.RETRY_WAIT.value
                session.commit()
            finally:
                session.close()
            raise ModelProviderError("temporary provider failure", retryable=True)

    runner = JobRunner(
        session_factory=SessionLocal,
        runtime=RetryWaitRuntime(),
        settings=_settings(),
    )
    runner.run_job(job.id)

    session = SessionLocal()
    try:
        stored_job = session.get(BackgroundJob, job.id)
        stored_run = session.get(AgentRun, run.id)
        assert stored_job is not None and stored_job.status == "queued"
        assert stored_job.attempt == 1
        assert stored_run is not None and stored_run.state == RunState.RETRY_WAIT.value
        assert session.scalar(
            select(AgentEvent).where(
                AgentEvent.run_id == run.id,
                AgentEvent.event_type == "run.retry_scheduled",
            )
        ) is not None
    finally:
        session.close()


def test_claim_next_recovers_an_expired_running_lease() -> None:
    run, job = _new_job("expired-lease-recovery")
    session = SessionLocal()
    try:
        stored_job = session.get(BackgroundJob, job.id)
        stored_run = session.get(AgentRun, run.id)
        assert stored_job is not None and stored_run is not None
        stored_job.status = "running"
        stored_job.priority = -1000
        stored_job.attempt = 1
        stored_job.lease_owner = "dead-worker"
        stored_job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        stored_run.state = RunState.EXECUTING.value
        session.commit()
    finally:
        session.close()

    runner = JobRunner(
        session_factory=SessionLocal,
        runtime=object(),
        settings=_settings(),
    )
    assert runner.claim_next() == job.id

    session = SessionLocal()
    try:
        stored_job = session.get(BackgroundJob, job.id)
        assert stored_job is not None
        assert stored_job.status == "running"
        assert stored_job.attempt == 2
        assert stored_job.lease_owner == runner.worker_id
        event = session.scalar(
            select(AgentEvent).where(
                AgentEvent.run_id == run.id,
                AgentEvent.event_type == "run.expired_lease_recovered",
            )
        )
        assert event is not None
        assert event.payload["provider_reasoning_restored"] is False
    finally:
        session.close()
