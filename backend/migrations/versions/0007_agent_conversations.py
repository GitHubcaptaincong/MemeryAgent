"""Add durable Agent conversations and turns.

Revision ID: 0007_agent_conversations
Revises: 0006_public_url_sources
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0007_agent_conversations"
down_revision: str | None = "0006_public_url_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    if "conversations" not in existing_tables:
        op.create_table(
            "conversations",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("title", sa.String(length=100), nullable=False),
            sa.Column("title_status", sa.String(length=32), nullable=False),
            sa.Column("turn_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
        op.create_index(
            "ix_conversations_user_updated", "conversations", ["user_id", "updated_at"]
        )
    if "conversation_turns" not in existing_tables:
        op.create_table(
            "conversation_turns",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("conversation_id", sa.Uuid(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("idempotency_key", sa.String(length=128), nullable=False),
            sa.Column("user_content", sa.Text(), nullable=False),
            sa.Column("source_id", sa.Uuid(), nullable=True),
            sa.Column("run_id", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "conversation_id", "idempotency_key", name="uq_conversation_turn_key"
            ),
            sa.UniqueConstraint(
                "conversation_id", "position", name="uq_conversation_turn_position"
            ),
        )
        op.create_index(
            "ix_conversation_turns_conversation_id", "conversation_turns", ["conversation_id"]
        )
        op.create_index(
            "ix_conversation_turns_conversation_created",
            "conversation_turns",
            ["conversation_id", "created_at"],
        )
        op.create_index("ix_conversation_turns_source_id", "conversation_turns", ["source_id"])
        op.create_index("ix_conversation_turns_run_id", "conversation_turns", ["run_id"])


def downgrade() -> None:
    op.drop_table("conversation_turns")
    op.drop_table("conversations")
