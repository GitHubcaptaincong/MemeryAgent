from __future__ import annotations

import os
from pathlib import Path

import pytest


TEST_DB = Path(__file__).resolve().parents[1] / "test-memory-agent.db"
os.environ["APP_DATABASE_URL"] = f"sqlite+pysqlite:///{TEST_DB.as_posix()}"
os.environ["APP_AUTO_CREATE_SCHEMA"] = "true"
os.environ["APP_INLINE_WORKER"] = "true"
os.environ["APP_MODEL_PROVIDER"] = "fake"


@pytest.fixture(scope="session", autouse=True)
def clean_test_database():
    if TEST_DB.exists():
        TEST_DB.unlink()
    yield
    from memory_agent.database import engine

    engine.dispose()
    if TEST_DB.exists():
        TEST_DB.unlink()
