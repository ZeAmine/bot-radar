"""Protocole et modèles de données de base pour les fournisseurs de prix de marché."""

from dataclasses import dataclass
from typing import Any, Protocol

from app.core.constants import MatchConfidence


@dataclass
class MarketOffer:
    """Offre d'occasion normalisée provenant d'un fournisseur."""

    provider_name: str
    item_price_cents: int
    shipping_price_cents: int | None
    delivered_price_cents: int
    source_url: str
    currency: str = "EUR"
    item_condition: str | None = None
    confidence: MatchConfidence = MatchConfidence.HIGH
    is_available: bool = True
    is_comparable: bool = True
    price_includes_shipping: bool = False
    raw_payload: dict[str, Any] | None = None


class BookMarketProvider(Protocol):
    """Interface abstraite que chaque fournisseur de marché doit respecter."""

    name: str

    async def get_offers(
        self,
        *,
        isbn13: str | None,
        title: str,
        author: str | None = None,
        edition: str | None = None,
    ) -> list[MarketOffer]:
        """Interroge le fournisseur et retourne une liste d'offres normalisées."""
        ...
