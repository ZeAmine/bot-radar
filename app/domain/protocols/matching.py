"""Contrat d'interface pour le moteur de matching déterministe."""

from typing import Protocol

from app.domain.entities.alert import AlertEntity
from app.domain.entities.listing import ListingEntity
from app.domain.entities.matching import MatchResult


class ListingMatcherProtocol(Protocol):
    """Évalue si une annonce correspond aux critères stricts d'une alerte."""

    def evaluate(
        self,
        listing: ListingEntity,
        alert: AlertEntity,
    ) -> MatchResult:
        """Exécute l'évaluation en mémoire (CPU pur, zéro I/O, zéro DB)."""
        ...
