"""add independent card reminders"""
from alembic import op
import sqlalchemy as sa

revision = "0009_card_reminders"
down_revision = "0008_knowledge_management"
branch_labels = None
depends_on = None

uuid_type = sa.Uuid(as_uuid=True)

def upgrade() -> None:
    op.create_table(
        "card_reminders",
        sa.Column("id", uuid_type, primary_key=True, nullable=False),
        sa.Column("user_id", uuid_type, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("review_card_id", uuid_type, sa.ForeignKey("review_cards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="scheduled"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_card_reminders_user_status_scheduled", "card_reminders", ["user_id", "status", "scheduled_for"])
    op.create_index("ix_card_reminders_card_status_scheduled", "card_reminders", ["review_card_id", "status", "scheduled_for"])
    with op.batch_alter_table("reminder_deliveries") as batch:
        batch.add_column(sa.Column("reminder_type", sa.String(16), nullable=False, server_default="daily"))
        batch.add_column(sa.Column("card_reminder_id", uuid_type, nullable=True))
        batch.alter_column("local_date", existing_type=sa.String(10), nullable=True)
        batch.create_foreign_key("fk_reminder_delivery_card_reminder", "card_reminders", ["card_reminder_id"], ["id"], ondelete="CASCADE")

def downgrade() -> None:
    with op.batch_alter_table("reminder_deliveries") as batch:
        batch.drop_constraint("fk_reminder_delivery_card_reminder", type_="foreignkey")
        batch.drop_column("card_reminder_id")
        batch.drop_column("reminder_type")
        batch.alter_column("local_date", existing_type=sa.String(10), nullable=False)
    op.drop_index("ix_card_reminders_card_status_scheduled", table_name="card_reminders")
    op.drop_index("ix_card_reminders_user_status_scheduled", table_name="card_reminders")
    op.drop_table("card_reminders")
