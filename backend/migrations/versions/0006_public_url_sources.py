"""Add origin metadata for long text and public URL sources.

Revision ID: 0006_public_url_sources
Revises: 0005_learning_assistance
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0006_public_url_sources"
down_revision: str | None = "0005_learning_assistance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    existing_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("sources")
    }
    with op.batch_alter_table("sources") as batch_op:
        if "origin_type" not in existing_columns:
            batch_op.add_column(
                sa.Column("origin_type", sa.String(length=32), server_default="text", nullable=False)
            )
        if "origin_url" not in existing_columns:
            batch_op.add_column(sa.Column("origin_url", sa.Text(), nullable=True))
        if "retrieved_at" not in existing_columns:
            batch_op.add_column(sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True))
        if "origin_content_hash" not in existing_columns:
            batch_op.add_column(
                sa.Column("origin_content_hash", sa.String(length=64), nullable=True)
            )


def downgrade() -> None:
    with op.batch_alter_table("sources") as batch_op:
        batch_op.drop_column("origin_content_hash")
        batch_op.drop_column("retrieved_at")
        batch_op.drop_column("origin_url")
        batch_op.drop_column("origin_type")
