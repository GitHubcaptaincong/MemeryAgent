"""Add review scheduling, immutable review events, and reminder preferences.

Revision ID: 0002_review_loop
Revises: 0001_initial_schema
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0002_review_loop"
down_revision: str | None = "0001_initial_schema"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_cards",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("draft_unit_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval_days", sa.Float(), nullable=False),
        sa.Column("review_count", sa.Integer(), nullable=False),
        sa.Column("lapse_count", sa.Integer(), nullable=False),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("scheduler_version", sa.String(length=64), nullable=False),
        sa.Column("scheduler_state", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["draft_unit_id"], ["draft_units.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("user_id", "draft_unit_id", name="uq_review_card_user_unit"),
    )
    op.create_index(
        "ix_review_cards_user_due",
        "review_cards",
        ["user_id", "status", "due_at"],
    )
    op.create_index("ix_review_cards_user_id", "review_cards", ["user_id"])

    op.create_table(
        "review_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("card_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["card_id"], ["review_cards.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_review_event_user_key"),
    )
    op.create_index("ix_review_events_user_id", "review_events", ["user_id"])
    op.create_index("ix_review_events_card_id", "review_events", ["card_id"])
    op.create_index(
        "ix_review_events_card_created", "review_events", ["card_id", "created_at"]
    )
    op.create_index(
        "ix_review_events_correlation",
        "review_events",
        ["correlation_id", "event_type"],
    )

    op.create_table(
        "reminder_preferences",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("preferred_time", sa.String(length=5), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("overdue_enabled", sa.Boolean(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_reminder_preference_user"),
    )


def downgrade() -> None:
    op.drop_table("reminder_preferences")
    op.drop_index("ix_review_events_correlation", table_name="review_events")
    op.drop_index("ix_review_events_card_created", table_name="review_events")
    op.drop_index("ix_review_events_card_id", table_name="review_events")
    op.drop_index("ix_review_events_user_id", table_name="review_events")
    op.drop_table("review_events")
    op.drop_index("ix_review_cards_user_id", table_name="review_cards")
    op.drop_index("ix_review_cards_user_due", table_name="review_cards")
    op.drop_table("review_cards")
