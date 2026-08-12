"""Add WeChat identities for per-user isolation.

Revision ID: 0004_wechat_identities
Revises: 0003_review_event_attempt_guard
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0004_wechat_identities"
down_revision: str | None = "0003_review_event_attempt_guard"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wechat_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("openid", sa.String(length=128), nullable=False),
        sa.Column("unionid", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_id", "openid", name="uq_wechat_identity_app_openid"),
        sa.UniqueConstraint("user_id", name="uq_wechat_identity_user"),
    )
    op.create_index(
        "ix_wechat_identities_unionid",
        "wechat_identities",
        ["unionid"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_wechat_identities_unionid", table_name="wechat_identities")
    op.drop_table("wechat_identities")
