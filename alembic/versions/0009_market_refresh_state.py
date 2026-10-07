"""Distingue la collecte tentée de la dernière observation de prix réelle."""

import sqlalchemy as sa

from alembic import op

revision: str = "0009_market_refresh_state"
down_revision: str | None = "0008_search_exhausted"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("book_market_cache", sa.Column("last_attempt_at", sa.DateTime(timezone=True)))
    op.add_column("book_market_cache", sa.Column("collection_status", sa.String(20)))
    op.add_column("book_market_cache", sa.Column("failed_provider_names", sa.JSON()))
    op.add_column("book_market_cache", sa.Column("current_offers", sa.JSON()))


def downgrade() -> None:
    for column in ("current_offers", "failed_provider_names", "collection_status", "last_attempt_at"):
        op.drop_column("book_market_cache", column)
