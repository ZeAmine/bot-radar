"""Autorisation de pause versionnée ; aucune preuve rétroactive."""

import sqlalchemy as sa

from alembic import op

revision: str = "0013_pause_actions"
down_revision: str | None = "0012_annual_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alerts", sa.Column("decision_version", sa.Integer(), nullable=False, server_default="0")
    )
    op.create_table(
        "pause_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("alert_id", sa.Uuid(), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("intent", sa.String(24), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("applied_version", sa.Integer()),
        sa.Column("update_id", sa.BigInteger()),
        sa.Column("callback_id", sa.String(128)),
    )
    op.create_index("ix_pause_actions_alert_id", "pause_actions", ["alert_id"])
    op.create_index("ix_pause_actions_issued_at", "pause_actions", ["issued_at"])


def downgrade() -> None:
    raise RuntimeError("L'ancien code accepterait à nouveau les pauses périmées.")
