"""Projection glissante additive ; aucune provenance historique inventée."""

import sqlalchemy as sa

from alembic import op

revision: str = "0012_annual_observations"
down_revision: str | None = "0011_notification_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, kind in [
        ("annual_low_cents", sa.Integer()),
        ("annual_low_observed_at", sa.DateTime(timezone=True)),
        ("annual_low_snapshot_id", sa.Uuid()),
        ("annual_low_provider", sa.String(100)),
        ("annual_first_at", sa.DateTime(timezone=True)),
        ("annual_last_at", sa.DateTime(timezone=True)),
        ("annual_count", sa.Integer()),
        ("annual_computed_at", sa.DateTime(timezone=True)),
        ("annual_expires_at", sa.DateTime(timezone=True)),
    ]:
        op.add_column("book_market_cache", sa.Column(name, kind, nullable=True))
    for name, kind in [
        ("observed_at", sa.DateTime(timezone=True)),
        ("annual_eligible", sa.Boolean()),
        ("observed_merchant", sa.String(100)),
    ]:
        op.add_column("market_price_snapshots", sa.Column(name, kind, nullable=True))
    op.create_index(
        "ix_book_market_cache_annual_expires_at", "book_market_cache", ["annual_expires_at"]
    )
    op.create_index(
        "ix_market_snapshots_book_observed", "market_price_snapshots", ["book_id", "observed_at"]
    )


def downgrade() -> None:
    raise RuntimeError("Une ancienne version pourrait comparer un minimum non vérifiable.")
