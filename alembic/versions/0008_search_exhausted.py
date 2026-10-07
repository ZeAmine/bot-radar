"""Verdict « aucune source ne catalogue cet ouvrage », persisté sur l'alerte.

Une série suivie par son nom (« Absolute Martian Manhunter ») n'a aucune fiche, et
ses annonces partaient en annonçant une recherche d'ouvrage **en cours**. Or la
recherche avait eu lieu, et sa réponse tenait en une ligne : toutes les autorités
interrogées avaient répondu, aucune ne connaissait l'ouvrage. L'attente éternelle
était le seul mensonge disponible — la donnée pour la dire existait déjà, elle
n'était simplement écrite nulle part.

La colonne porte la **date** du relevé qui a établi le verdict, jamais un drapeau :
un fait daté se lit comme une observation, et il se remplace au relevé suivant. Elle
n'est pas remplie par cette migration — l'écrire ici serait une seconde
implémentation du verdict, qui finirait par contredire la première. Elle se pose à
la prochaine résolution de fond de chaque alerte concernée, et s'efface dès qu'une
notice est liée.

Une autorité muette ne pose rien : « personne ne le connaît » exige que tout le
monde ait répondu (règle pure `is_catalogue_exhausted`, ADR-041).

Revision ID: 0008_search_exhausted
Revises: 0007_series_total
Create Date: 2026-10-07 09:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_search_exhausted"
down_revision: str | None = "0007_series_total"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "alerts",
        sa.Column("book_search_exhausted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("alerts", "book_search_exhausted_at")
