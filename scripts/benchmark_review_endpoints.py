"""Benchmark key review endpoints against a seeded throwaway SQLite database.

Seeds a realistic review workload (sources with full text, drafts, due cards,
rated history, one legacy-scheduler card), then measures the endpoints that
the review page calls, using the perf log lines emitted by PerfLogMiddleware.

This is a development tool only; it never touches production data.

Usage (from repo root):
    .venv/Scripts/python.exe scripts/benchmark_review_endpoints.py
    .venv/Scripts/python.exe scripts/benchmark_review_endpoints.py --rtt-ms 40
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", type=int, default=25, help="number of due cards to seed")
    parser.add_argument("--rated", type=int, default=5, help="cards with real rated history")
    parser.add_argument("--rtt-ms", type=float, default=0.0, help="simulated per-query RTT in ms")
    parser.add_argument("--repeat", type=int, default=3, help="measured repetitions per endpoint")
    return parser.parse_args()


ARGS = parse_args()

# Environment must be pinned before memory_agent builds its engine/settings.
os.environ["APP_DATABASE_URL"] = f"sqlite+pysqlite:///{(Path(tempfile.gettempdir()) / f'mem-agent-bench-{os.getpid()}.db').as_posix()}"
os.environ["APP_AUTH_MODE"] = "local"
os.environ["APP_AUTO_CREATE_SCHEMA"] = "true"
os.environ["APP_MODEL_PROVIDER"] = "fake"
os.environ["APP_INLINE_WORKER"] = "true"

from memory_agent.config import get_settings  # noqa: E402
from memory_agent.database import SessionLocal, engine  # noqa: E402
from memory_agent.main import app  # noqa: E402
from memory_agent.models import (  # noqa: E402
    AgentProfile,
    AgentRun,
    DraftUnit,
    KnowledgeDraft,
    ReviewCard,
    ReviewEvent,
    Source,
    User,
    utc_now,
)
from memory_agent.scheduler import FsrsReviewScheduler  # noqa: E402

if ARGS.rtt_ms > 0:
    from sqlalchemy import event

    @event.listens_for(engine, "before_cursor_execute")
    def _simulate_network_rtt(*_args) -> None:
        time.sleep(ARGS.rtt_ms / 1000)


PERF_LINE = re.compile(
    r"^perf (\S+) (\S+) status=(\S+) total_ms=([\d.]+) auth_ms=([\d.]+) "
    r"db_ms=([\d.]+) business_ms=([\d.]+) sql_count=(\d+)$"
)

UNITS_PER_SOURCE = 5


def seed_local_review_workload(user_id, profile_id) -> list[ReviewCard]:
    scheduler = FsrsReviewScheduler()
    due_at = utc_now() - timedelta(minutes=1)
    cards: list[ReviewCard] = []
    with SessionLocal() as session:
        sources_needed = (ARGS.cards + UNITS_PER_SOURCE - 1) // UNITS_PER_SOURCE
        for source_index in range(sources_needed):
            source = Source(
                id=uuid4(),
                user_id=user_id,
                title=f"Bench source {source_index}",
                learning_goal="Benchmark workload",
                raw_content=("知识密度足够的 benchmark 原文。" * 300),
                content_type="text",
                content_hash=uuid4().hex,
                char_count=300 * 15,
            )
            run = AgentRun(
                id=uuid4(),
                user_id=user_id,
                profile_id=profile_id,
                source_id=source.id,
                idempotency_key=f"bench-{source_index}",
            )
            draft = KnowledgeDraft(
                id=uuid4(),
                user_id=user_id,
                source_id=source.id,
                run_id=run.id,
                status="confirmed",
                confirmed_at=utc_now(),
                title=f"Bench draft {source_index}",
                learning_goal="Benchmark workload",
                agent_summary={"overview": "benchmark"},
            )
            session.add_all([source, run, draft])
            session.flush()
            for unit_position in range(UNITS_PER_SOURCE):
                unit = DraftUnit(
                    id=uuid4(),
                    draft_id=draft.id,
                    position=unit_position,
                    title=f"Unit {source_index}-{unit_position}",
                    learning_objective="记住机制",
                    explanation="benchmark",
                    question=f"问题 {source_index}-{unit_position}：这个机制为什么成立？",
                    answer_key=["要点一", "要点二"],
                    hints=["提示一"],
                )
                card_id = uuid4()
                card = ReviewCard(
                    id=card_id,
                    user_id=user_id,
                    draft_unit_id=unit.id,
                    status="active",
                    due_at=due_at,
                    scheduler_version=scheduler.version,
                    scheduler_state=scheduler.initial_state(card_id=card_id, due_at=due_at),
                )
                session.add_all([unit, card])
                cards.append(card)
        session.commit()
    return cards


def make_one_card_legacy(card_id) -> None:
    with SessionLocal() as session:
        card = session.get(ReviewCard, card_id)
        card.scheduler_version = "mvp-review-v1"
        card.scheduler_state = {"algorithm": "transparent_mvp_steps"}
        card.review_count = 1
        session.add(
            ReviewEvent(
                user_id=card.user_id,
                card_id=card.id,
                correlation_id=uuid4(),
                event_type="review_rated",
                idempotency_key=f"bench-legacy-{card.id}",
                payload_json={
                    "rating": 3,
                    "reviewed_at": (utc_now() - timedelta(days=2)).isoformat(),
                    "next_due_at": card.due_at.isoformat(),
                    "interval_days": 2.0,
                },
            )
        )
        session.commit()


def redate_all_cards_due(user_id) -> None:
    with SessionLocal() as session:
        from sqlalchemy import update

        session.execute(
            update(ReviewCard)
            .where(ReviewCard.user_id == user_id, ReviewCard.status == "active")
            .values(due_at=utc_now() - timedelta(minutes=1))
        )
        session.commit()


class PerfCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        match = PERF_LINE.match(record.getMessage())
        if match:
            method, path, status, total, auth, db, business, sql = match.groups()
            self.records.append(
                {
                    "method": method,
                    "path": path,
                    "status": status,
                    "total_ms": float(total),
                    "auth_ms": float(auth),
                    "db_ms": float(db),
                    "business_ms": float(business),
                    "sql_count": int(sql),
                }
            )


def summarize(capture: PerfCapture, method: str, path: str) -> dict[str, object] | None:
    rows = [
        item
        for item in capture.records
        if item["method"] == method and item["path"].startswith(path)
    ]
    if not rows:
        return None
    medians = {
        key: statistics.median(item[key] for item in rows)
        for key in ("total_ms", "auth_ms", "db_ms", "business_ms")
    }
    sql_counts = [item["sql_count"] for item in rows]
    return {
        "label": f"{method} {path}",
        **medians,
        "sql_count": sorted(sql_counts)[len(sql_counts) // 2],
        "runs": len(rows),
        "totals": [round(item["total_ms"], 1) for item in rows],
    }


def main() -> int:
    capture = PerfCapture()
    logging.getLogger("memory_agent.perf").addHandler(capture)
    logging.getLogger("memory_agent.perf").setLevel(logging.INFO)

    from fastapi.testclient import TestClient

    settings = get_settings()
    cards = []
    with TestClient(app) as client:
        with SessionLocal() as session:
            user = session.get(User, settings.local_user_id)
            from sqlalchemy import select

            profile_id = session.scalar(
                select(AgentProfile.id).where(AgentProfile.user_id == user.id).limit(1)
            )
            user_id = user.id
        cards = seed_local_review_workload(user_id, profile_id)
        # Leave one card on the legacy scheduler so the replay path stays covered.
        make_one_card_legacy(cards[-1].id)

        # Create legit rated history through the API for a few cards.
        for index, card in enumerate(cards[: ARGS.rated]):
            answer = client.post(
                f"/api/v1/review/cards/{card.id}/answers",
                json={"answer": "机制成立因为……", "idempotency_key": f"bench-a-{index}"},
            )
            assert answer.status_code == 201, answer.text
            rating = client.post(
                f"/api/v1/review/cards/{card.id}/ratings",
                json={
                    "attempt_id": answer.json()["attempt_id"],
                    "rating": (index % 4) + 1,
                    "idempotency_key": f"bench-r-{index}",
                },
            )
            assert rating.status_code == 200, rating.text
        redate_all_cards_due(user_id)

        def measure(method: str, path: str, json_body=None):
            for _ in range(ARGS.repeat):
                response = client.request(method, path, json=json_body)
                assert response.status_code in (200, 201), (path, response.status_code, response.text)

        measure("GET", "/api/v1/review/queue?limit=100")
        measure("GET", "/api/v1/review/overview")
        measure("GET", "/api/v1/review/history?limit=10")
        measure("GET", "/api/v1/review/daily-plan")
        measure("GET", "/api/v1/review/insights?trend_days=30&forecast_days=14&weak_limit=5")

        # Rate fresh cards through the API to measure the mutation path.
        fresh = cards[ARGS.rated : ARGS.rated + ARGS.repeat]
        for index, card in enumerate(fresh):
            answer = client.post(
                f"/api/v1/review/cards/{card.id}/answers",
                json={"answer": "答案内容……", "idempotency_key": f"bench-flow-a-{index}"},
            )
            assert answer.status_code == 201, answer.text
            result = client.post(
                f"/api/v1/review/cards/{card.id}/ratings",
                json={
                    "attempt_id": answer.json()["attempt_id"],
                    "rating": 3,
                    "idempotency_key": f"bench-flow-r-{index}",
                },
            )
            assert result.status_code == 200, result.text

    rows = [
        summarize(capture, "GET", "/api/v1/review/queue"),
        summarize(capture, "GET", "/api/v1/review/overview"),
        summarize(capture, "GET", "/api/v1/review/history"),
        summarize(capture, "GET", "/api/v1/review/daily-plan"),
        summarize(capture, "GET", "/api/v1/review/insights"),
        summarize(capture, "POST", "/api/v1/review/cards/"),
    ]
    rtt_note = f" (simulated RTT {ARGS.rtt_ms:.0f}ms per query)" if ARGS.rtt_ms > 0 else " (no RTT simulation)"
    print(f"\nBenchmark: {ARGS.cards} due cards, {ARGS.rated} pre-rated{rtt_note}, repeat={ARGS.repeat}\n")
    header = f"{'endpoint':46} {'total':>9} {'auth':>8} {'db':>8} {'business':>9} {'sql':>5}"
    print(header)
    print("-" * len(header))
    for row in rows:
        if row is None:
            continue
        print(
            f"{row['label']:46} {row['total_ms']:>8.1f} {row['auth_ms']:>8.1f} "
            f"{row['db_ms']:>8.1f} {row['business_ms']:>9.1f} {row['sql_count']:>5}"
        )
    ratings_rows = [r for r in capture.records if r["method"] == "POST"]
    if ratings_rows:
        print("\nPOST runs (per repetition):")
        for item in ratings_rows:
            print(
                f"  {item['method']} {item['path']} total={item['total_ms']:.1f}ms "
                f"sql_count={item['sql_count']}"
            )

    engine.dispose()
    db_path = Path(os.environ["APP_DATABASE_URL"].split("///", 1)[1])
    if db_path.exists():
        db_path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
