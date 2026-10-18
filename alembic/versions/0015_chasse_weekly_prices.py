import sqlalchemy as sa

from alembic import op

revision: str = "0015_chasse_weekly_prices"
down_revision: str = "0014_alert_market_freshness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("book_market_cache", sa.Column("source_history", sa.JSON()))
    op.add_column("book_market_cache", sa.Column("refresh_state", sa.JSON()))
    op.add_column("book_market_cache", sa.Column("next_refresh_at", sa.DateTime(timezone=True)))
    op.create_index("ix_book_market_cache_next_refresh_at", "book_market_cache", ["next_refresh_at"])


def downgrade() -> None:
    op.drop_index("ix_book_market_cache_next_refresh_at", table_name="book_market_cache")
    for name in ("next_refresh_at", "refresh_state", "source_history"):
        op.drop_column("book_market_cache", name)
