"""Prevent duplicate events for one review attempt.

Revision ID: 0003_review_event_attempt_guard
Revises: 0002_review_loop
"""
from typing import Sequence

from alembic import op


revision: str = "0003_review_event_attempt_guard"
down_revision: str | None = "0002_review_loop"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("review_events") as batch_op:
        batch_op.create_unique_constraint(
            "uq_review_event_card_attempt_type",
            ["card_id", "correlation_id", "event_type"],
        )


def downgrade() -> None:
    with op.batch_alter_table("review_events") as batch_op:
        batch_op.drop_constraint(
            "uq_review_event_card_attempt_type",
            type_="unique",
        )
