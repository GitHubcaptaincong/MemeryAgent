from __future__ import annotations

import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from memory_agent.agent import AgentRuntime
from memory_agent.config import Settings
from memory_agent.events import append_event, transition_run
from memory_agent.models import AgentRun, BackgroundJob, RunState


class JobRunner:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        runtime: AgentRuntime,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.runtime = runtime
        self.settings = settings
        self.worker_id = f"{socket.gethostname()}-{id(self)}"

    def run_job(self, job_id: UUID) -> None:
        session = self.session_factory()
        try:
            job = session.get(BackgroundJob, job_id)
            if job is None or job.status == "succeeded":
                return
            if job.status == "running" and job.lease_expires_at:
                lease = job.lease_expires_at
                if lease.tzinfo is None:
                    lease = lease.replace(tzinfo=UTC)
                if lease > datetime.now(UTC) and job.lease_owner != self.worker_id:
                    return
            if job.status != "running" or job.lease_owner != self.worker_id:
                self._mark_running(job)
                session.commit()
            if job.job_type != "agent_run" or job.run_id is None:
                raise ValueError(f"unsupported job type: {job.job_type}")
            self.runtime.execute(job.run_id)
            job = session.get(BackgroundJob, job_id)
            if job is not None:
                job.status = "succeeded"
                job.lease_expires_at = None
                session.commit()
        except Exception as exc:
            session.rollback()
            job = session.get(BackgroundJob, job_id)
            if job is not None:
                job.last_error = str(exc)
                job.lease_expires_at = None
                run = session.get(AgentRun, job.run_id) if job.run_id else None
                can_retry = (
                    run is not None
                    and run.state == RunState.RETRY_WAIT.value
                    and job.attempt < job.max_attempts
                )
                if can_retry:
                    delay_seconds = min(30, 2**job.attempt)
                    job.status = "queued"
                    job.available_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
                    append_event(
                        session,
                        run,
                        "run.retry_scheduled",
                        {
                            "attempt": job.attempt + 1,
                            "max_attempts": job.max_attempts,
                            "delay_seconds": delay_seconds,
                            "strategy": "fresh_context_replay",
                        },
                    )
                    session.commit()
                else:
                    job.status = "failed"
                    if run is not None and run.state == RunState.RETRY_WAIT.value:
                        run.stop_reason = "model_provider_retry_exhausted"
                        append_event(
                            session,
                            run,
                            "run.failed",
                            {
                                "error_code": type(exc).__name__,
                                "message": str(exc),
                                "attempts": job.attempt,
                            },
                        )
                        transition_run(session, run, RunState.FAILED)
                    else:
                        session.commit()
        finally:
            session.close()

    def claim_next(self) -> UUID | None:
        session = self.session_factory()
        try:
            now = datetime.now(UTC)
            job = session.scalar(
                select(BackgroundJob)
                .where(
                    or_(
                        and_(
                            BackgroundJob.status == "queued",
                            BackgroundJob.available_at <= now,
                        ),
                        and_(
                            BackgroundJob.status == "running",
                            BackgroundJob.lease_expires_at.is_not(None),
                            BackgroundJob.lease_expires_at <= now,
                        ),
                    )
                )
                .order_by(BackgroundJob.priority, BackgroundJob.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            recovered_lease = job.status == "running"
            previous_owner = job.lease_owner
            self._mark_running(job)
            if recovered_lease and job.run_id is not None:
                run = session.get(AgentRun, job.run_id)
                if run is not None and run.state not in {
                    RunState.AWAITING_USER.value,
                    RunState.COMPLETED.value,
                    RunState.FAILED.value,
                    RunState.CANCELLED.value,
                }:
                    append_event(
                        session,
                        run,
                        "run.expired_lease_recovered",
                        {
                            "previous_lease_owner": previous_owner,
                            "attempt": job.attempt,
                            "strategy": "fresh_context_replay",
                            "provider_reasoning_restored": False,
                        },
                    )
            session.commit()
            return job.id
        finally:
            session.close()

    def _mark_running(self, job: BackgroundJob) -> None:
        now = datetime.now(UTC)
        job.status = "running"
        job.attempt += 1
        job.lease_owner = self.worker_id
        job.heartbeat_at = now
        job.lease_expires_at = now + timedelta(seconds=self.settings.agent_max_seconds + 30)

    def run_forever(self) -> None:
        while True:
            job_id = self.claim_next()
            if job_id is None:
                time.sleep(self.settings.worker_poll_seconds)
                continue
            self.run_job(job_id)
