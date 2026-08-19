"""Add knowledge management metadata to confirmed drafts.

Revision ID: 0008_knowledge_management
Revises: 0007_agent_conversations
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0008_knowledge_management"
down_revision: str | None = "0007_agent_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "title" not in _columns("knowledge_drafts"):
        op.add_column("knowledge_drafts", sa.Column("title", sa.String(length=300)))
    op.execute(
        sa.text(
            "UPDATE knowledge_drafts SET title = "
            "(SELECT sources.title FROM sources WHERE sources.id = knowledge_drafts.source_id) "
            "WHERE title IS NULL"
        )
    )
    if "status" not in _columns("draft_units"):
        op.add_column(
            "draft_units",
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        )
    op.execute(sa.text("UPDATE draft_units SET status = 'active' WHERE status IS NULL"))
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("draft_units")}
    if "ix_draft_units_draft_status" not in indexes:
        op.create_index(
            "ix_draft_units_draft_status", "draft_units", ["draft_id", "status"]
        )


def downgrade() -> None:
    op.drop_index("ix_draft_units_draft_status", table_name="draft_units")
    op.drop_column("draft_units", "status")
    op.drop_column("knowledge_drafts", "title")
