"""Lightweight per-request performance instrumentation.

Tracks wall-clock time, auth time, SQL time and SQL query count for a small
set of key review endpoints and logs one line per request. Uses only the
standard library + SQLAlchemy events; no external monitoring dependency.

The middleware is a pure ASGI middleware (not BaseHTTPMiddleware) so the
SSE endpoint (/runs/{id}/events/stream) is never buffered or wrapped.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from sqlalchemy.engine import Engine
from sqlalchemy import event

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("memory_agent.perf")

_TRACKED_GET_PATHS = frozenset(
    {
        "/api/v1/review/queue",
        "/api/v1/review/overview",
        "/api/v1/review/history",
        "/api/v1/review/daily-plan",
        "/api/v1/review/insights",
    }
)
_TRACKED_MUTATION_SUFFIXES = ("/ratings", "/answers")
_PERF_LOG_FORMAT = (
    "perf %s %s status=%s total_ms=%.1f auth_ms=%.1f db_ms=%.1f "
    "business_ms=%.1f sql_count=%d"
)


@dataclass
class RequestPerf:
    path: str = ""
    method: str = ""
    auth_ms: float = 0.0
    # SQL time spent inside auth (subset of db_ms, used to derive business_ms)
    auth_db_ms: float = 0.0
    db_ms: float = 0.0
    sql_count: int = 0
    in_auth: bool = False
    _sql_started: float | None = field(default=None, repr=False)

    def add_sql(self, duration_ms: float) -> None:
        self.db_ms += duration_ms
        self.sql_count += 1
        if self.in_auth:
            self.auth_db_ms += duration_ms


_perf_var: ContextVar[RequestPerf | None] = ContextVar("request_perf", default=None)


def current_request_perf() -> RequestPerf | None:
    return _perf_var.get()


@contextmanager
def track_auth():
    """Attribute wall time (and nested SQL time) spent in identity resolution."""
    perf = _perf_var.get()
    if perf is None:
        yield
        return
    perf.in_auth = True
    started = time.perf_counter()
    try:
        yield
    finally:
        perf.auth_ms += (time.perf_counter() - started) * 1000
        perf.in_auth = False


def _should_track(path: str, method: str) -> bool:
    if method == "OPTIONS":
        return False
    if path in _TRACKED_GET_PATHS:
        return True
    return path.startswith("/api/v1/review/cards/") and path.endswith(
        _TRACKED_MUTATION_SUFFIXES
    )


def register_sql_perf_listeners(engine: Engine) -> None:
    @event.listens_for(engine, "before_cursor_execute")
    def _before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        perf = _perf_var.get()
        if perf is None:
            return
        perf._sql_started = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        perf = _perf_var.get()
        started = perf._sql_started if perf else None
        if perf is not None:
            perf._sql_started = None
        if perf is None or started is None:
            return
        perf.add_sql((time.perf_counter() - started) * 1000)


class PerfLogMiddleware:
    """Log phase timings for key review endpoints. Pure ASGI pass-through."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _should_track(
            scope.get("path", ""), scope.get("method", "")
        ):
            await self.app(scope, receive, send)
            return

        perf = RequestPerf(
            path=scope.get("path", ""), method=scope.get("method", "")
        )
        _perf_var.set(perf)
        started = time.perf_counter()
        status_code = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            total_ms = (time.perf_counter() - started) * 1000
            business_ms = max(0.0, total_ms - perf.auth_ms - (perf.db_ms - perf.auth_db_ms))
            logger.info(
                _PERF_LOG_FORMAT,
                perf.method,
                perf.path,
                status_code or "-",
                total_ms,
                perf.auth_ms,
                perf.db_ms,
                business_ms,
                perf.sql_count,
            )
            _perf_var.set(None)
