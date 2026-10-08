"""Ajoute une demande de préparation nullable, sans reclasser les pauses existantes."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010_alert_preparation"
down_revision: str | None = "0009_market_refresh_state"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "alerts",
        sa.Column(
            "preparation", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
        ),
    )


def downgrade() -> None:
    raise RuntimeError("La préparation durable doit être conservée : aucun downgrade destructeur.")
