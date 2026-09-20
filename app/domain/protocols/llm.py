"""Contrat d'interface pour le fournisseur d'analyse IA."""

from typing import Protocol

from app.domain.entities.analysis import ListingAnalysisResult
from app.domain.enums import CategorySlug


class LLMAnalysisProviderProtocol(Protocol):
    """Analyse sémantique d'une annonce (état réel, pièges, complétude)."""

    async def analyze_listing(
        self,
        title: str,
        description: str,
        price_cents: int,
        declared_condition: str | None,
        category: CategorySlug,
        image_urls: list[str] | None = None,
    ) -> ListingAnalysisResult | None:
        """Exécute l'analyse avec cache Redis et fallback gracieux (None en cas d'échec)."""
        ...
