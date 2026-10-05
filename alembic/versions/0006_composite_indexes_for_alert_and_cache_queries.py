"""Index composites pour les requêtes d'alertes et de cache.

Deux requêtes fréquentes ne sont pas optimisées par les index existants :
1. `list_by_user` filtre sur `(user_id, status)` — l'index sur `user_id` seul
   oblige à scanner toutes les alertes d'un utilisateur avant de filtrer par statut.
2. `get_books_needing_price_refresh` filtre sur `checked_at IS NULL` — sans index
   sur `checked_at`, PostgreSQL doit scanner toute la table.

Revision ID: 0006_composite_indexes_for_alert_and_cache_queries
Revises: 0005_correct_historical_low
Create Date: 2026-10-05 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_composite_indexes_for_alert_and_cache_queries"
down_revision: str | None = "0005_correct_historical_low"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_alerts_user_id_status", "alerts", ["user_id", "status"], unique=False
    )
    op.create_index(
        "ix_book_market_cache_checked_at", "book_market_cache", ["checked_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_book_market_cache_checked_at", table_name="book_market_cache")
    op.drop_index("ix_alerts_user_id_status", table_name="alerts")
