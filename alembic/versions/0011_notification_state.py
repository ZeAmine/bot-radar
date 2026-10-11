"""Progression de notification sans reconstruire les preuves historiques."""
import sqlalchemy as sa

from alembic import op

revision: str = "0011_notification_state"
down_revision: str | None = "0010_alert_preparation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    columns = [
        sa.Column("notification_state", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("attempt_count", sa.Integer(), nullable=True),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("notification_reason", sa.String(64), nullable=True),
    ]
    for column in columns:
        op.add_column("listing_matches", column)
    op.execute(sa.text("UPDATE listing_matches SET notification_state = 'notified' "
                       "WHERE notified_at IS NOT NULL"))
    op.execute(sa.text("UPDATE listing_matches SET notification_reason = 'legacy_unclassified' "
                       "WHERE notified_at IS NULL"))
    op.create_index("ix_listing_matches_next_attempt_at", "listing_matches", ["next_attempt_at"])
    op.create_index("ix_listing_matches_recovery", "listing_matches",
                    ["notification_state", "next_attempt_at", "created_at"])


def downgrade() -> None:
    raise RuntimeError("L'ancien code peut renvoyer les inconnus : downgrade destructeur interdit.")
