from __future__ import annotations

import asyncio
import logging
import threading

from memory_agent.config import get_settings
from memory_agent.database import SessionLocal
from memory_agent.reminder_worker import ReminderDispatcher
from memory_agent.runtime import get_job_runner


logger = logging.getLogger(__name__)


def _run_reminder_dispatcher(stop_event: threading.Event) -> None:
    dispatcher = ReminderDispatcher(
        session_factory=SessionLocal,
        settings=get_settings(),
    )
    while not stop_event.is_set():
        try:
            asyncio.run(dispatcher.run_until_stopped(stop_event))
        except Exception:
            logger.exception("reminder dispatcher stopped unexpectedly; restarting")
            stop_event.wait(1)


def run() -> None:
    stop_reminder_worker = threading.Event()
    reminder_thread = threading.Thread(
        target=_run_reminder_dispatcher,
        args=(stop_reminder_worker,),
        name="memory-agent-reminder-worker",
        daemon=True,
    )
    reminder_thread.start()
    logger.info("memory agent worker started")
    try:
        get_job_runner().run_forever()
    finally:
        stop_reminder_worker.set()
        reminder_thread.join(timeout=2)
        logger.info("memory agent worker stopped")


if __name__ == "__main__":
    run()
