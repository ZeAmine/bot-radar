"""Total de série persisté sur l'alerte.

Une alerte série globale (sans ISBN) a ses volumes cotés un par un en base, mais
son total n'existait nulle part : chaque écran recomposait — ou n'affichait rien.
La colonne est réécrite à chaque écriture de cote (`persist_market_result`, chemin
unique), jamais remplie par cette migration : un total calculé ici le serait selon
une seconde implémentation de la règle, qui finirait par contredire la première.

La colonne se remplit donc d'elle-même aux prochaines passes de cotation
(job glissant toutes les 30 min, pleine passe 07:00/19:00 Europe/Paris) ;
l'affichage se contente d'omettre la ligne tant que le total est NULL.

Revision ID: 0007_series_total
Revises: 0006_composite_indexes
Create Date: 2026-10-06 18:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_series_total"
down_revision: str | None = "0006_composite_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("alerts", sa.Column("series_total_cents", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("alerts", "series_total_cents")
