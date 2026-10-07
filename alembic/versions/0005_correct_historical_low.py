"""Abaisse les cotes 1 an que nos propres releves contredisent.

Revision ID: 0005_correct_historical_low
Revises: 0004_alert_books_book_id_index
Create Date: 2026-10-04 14:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_correct_historical_low"
down_revision: str | None = "0004_alert_books_book_id_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Une cote 1 an superieure a un prix livre que nous avons nous-memes releve pour
# le meme livre est une cote fausse : neuf livres etaient dans ce cas au deploiement
# (dont Dead Train, 22,90 EUR contre 13,57 EUR observes). La correction n'abaisse que,
# ne cree jamais de cote et ne touche pas aux livres qui n'en ont pas.
_SQL = """
UPDATE book_market_cache AS c
SET historical_low_1y_cents = observed.min_delivered,
    updated_at = now()
FROM (
    SELECT book_id, min(delivered_price_cents) AS min_delivered
    FROM market_price_snapshots
    WHERE is_comparable IS TRUE AND delivered_price_cents > 0
    GROUP BY book_id
) AS observed
WHERE observed.book_id = c.book_id
  AND c.historical_low_1y_cents IS NOT NULL
  AND observed.min_delivered < c.historical_low_1y_cents
"""


def upgrade() -> None:
    # Le schéma initial ne créait pas ces colonnes, pourtant déjà présentes
    # sur l'ancienne production. Une installation vierge doit aussi passer.
    op.execute(
        "ALTER TABLE book_market_cache "
        "ADD COLUMN IF NOT EXISTS historical_low_1y_cents INTEGER"
    )
    op.execute(
        "ALTER TABLE book_market_cache "
        "ADD COLUMN IF NOT EXISTS historical_low_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(_SQL)


def downgrade() -> None:
    """Rattrapage irreversible par nature : une cote abaissee ne se reinvente pas.

    ``downgrade`` ne fait rien plutot que de restaurer une valeur que l'on ne
    connait pas. Reinitialiser le cache force au pire une nouvelle cotation.
    """
