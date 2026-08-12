"""Create the stage-one schema.

Revision ID: 0001_initial_schema
Revises: None
"""
from typing import Sequence

from alembic import op

from memory_agent import models  # noqa: F401
from memory_agent.database import Base


revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


# Keep the initial revision pinned to its original table set. Calling
# create_all() for the entire live metadata makes a fresh database create tables
# from later revisions before Alembic reaches those revisions.
STAGE_ONE_TABLE_NAMES = (
    "users",
    "agent_profiles",
    "sources",
    "source_chunks",
    "agent_runs",
    "agent_events",
    "agent_checkpoints",
    "tool_invocations",
    "knowledge_drafts",
    "draft_units",
    "draft_source_spans",
    "memory_candidates",
    "memory_items",
    "memory_evidence",
    "memory_relations",
    "skills",
    "skill_versions",
    "retrieval_documents",
    "background_jobs",
)


def _stage_one_tables():
    return [Base.metadata.tables[name] for name in STAGE_ONE_TABLE_NAMES]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    Base.metadata.create_all(bind=bind, tables=_stage_one_tables())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=_stage_one_tables())
