"""Initialisation des alertes et provenance des actualisations de marché."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0014_alert_market_freshness"
down_revision: str | None = "0013_pause_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alerts", sa.Column("listing_baseline", sa.JSON().with_variant(JSONB, "postgresql")))
    op.add_column("alerts", sa.Column("catalogue_refresh_attempted_at", sa.DateTime(timezone=True)))
    op.add_column("market_price_snapshots", sa.Column(
        "is_historical", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    for name in ("last_successful_collection_at", "historical_checked_at"):
        op.add_column("book_market_cache", sa.Column(name, sa.DateTime(timezone=True)))
    # La dernière offre datée garde son sens ; aucune réussite n'est inventée pour un échec.
    op.execute("""UPDATE book_market_cache SET last_successful_collection_at = checked_at
                  WHERE checked_at IS NOT NULL AND (
                      collection_status IN ('complete', 'empty') OR
                      (collection_status = 'partial' AND lowest_delivered_price_cents > 0))""")


def downgrade() -> None:
    raise RuntimeError("Les preuves de collecte et les exclusions initiales doivent être conservées.")
