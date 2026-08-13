"""Add reminder delivery state and review evaluation preference.

Revision ID: 0005_learning_assistance
Revises: 0004_wechat_identities
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0005_learning_assistance"
down_revision: str | None = "0004_wechat_identities"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reminder_preferences") as batch_op:
        batch_op.add_column(
            sa.Column(
                "ai_evaluation_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )

    op.create_index(
        "ix_review_events_user_type_created",
        "review_events",
        ["user_id", "event_type", "created_at"],
        unique=False,
    )

    op.create_table(
        "reminder_subscription_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_reminder_grant_user_key"),
    )
    op.create_index(
        "ix_reminder_grants_user_template_status",
        "reminder_subscription_grants",
        ["user_id", "template_id", "status", "created_at"],
        unique=False,
    )

    op.create_table(
        "reminder_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=True),
        sa.Column("template_id", sa.String(length=128), nullable=False),
        sa.Column("local_date", sa.String(length=10), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["grant_id"], ["reminder_subscription_grants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "template_id",
            "local_date",
            name="uq_reminder_delivery_user_template_date",
        ),
    )
    op.create_index(
        "ix_reminder_deliveries_status_scheduled",
        "reminder_deliveries",
        ["status", "scheduled_for"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reminder_deliveries_status_scheduled", table_name="reminder_deliveries")
    op.drop_table("reminder_deliveries")
    op.drop_index(
        "ix_reminder_grants_user_template_status",
        table_name="reminder_subscription_grants",
    )
    op.drop_table("reminder_subscription_grants")
    op.drop_index("ix_review_events_user_type_created", table_name="review_events")
    with op.batch_alter_table("reminder_preferences") as batch_op:
        batch_op.drop_column("ai_evaluation_enabled")
