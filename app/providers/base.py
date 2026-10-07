"""Protocole et modèles de données de base pour les fournisseurs de prix de marché."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.constants import MatchConfidence
from app.utils.isbn import is_valid_isbn, normalize_isbn


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


class MarketOffers(list[MarketOffer]):
    """Offres collectées et sources en échec ; une liste vide n'est pas un diagnostic."""

    def __init__(
        self,
        offers: Iterable[MarketOffer] = (),
        *,
        failed_providers: tuple[str, ...] = (),
    ) -> None:
        super().__init__(offers)
        self.failed_providers = failed_providers


def normalize_search_isbn(raw_isbn: str | None) -> str | None:
    """Retourne un ISBN interrogeable, ou ``None`` s'il est absent ou invalide.

    Les fournisseurs recherchant par ISBN ne savent renvoyer que l'ouvrage codé
    dans ce code : un ISBN mal saisi (faute de frappe, préfixe recomposé) ferait
    coter le mauvais livre sous le bon identifiant. La clé de contrôle est donc
    vérifiée avant toute requête réseau.
    """
    candidate = normalize_isbn(raw_isbn)
    if candidate and is_valid_isbn(candidate):
        return candidate
    return None


def _positive_cents(value: Any) -> int | None:
    """Convertit une valeur de payload en centimes strictement positifs, sinon ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        cents = int(float(value))
    except (TypeError, ValueError):
        return None
    return cents if cents > 0 else None


def extract_best_historical_low(offers: list[MarketOffer]) -> int | None:
    """Retourne le plus bas historique certifié sur 1 an parmi les offres fournisseurs.

    Les payloads bruts peuvent exposer ``historical_low_1y_cents`` issu des agrégateurs
    (ex : Chasse-aux-livres). Une valeur absente, non numérique ou nulle est ignorée :
    elle ne doit jamais devenir une cote ni faire échouer le rafraîchissement du livre.
    """
    hist_lows: list[int] = [
        cents
        for offer in offers
        if offer.raw_payload
        for cents in [_positive_cents(offer.raw_payload.get("historical_low_1y_cents"))]
        if cents is not None
    ]
    return min(hist_lows) if hist_lows else None


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
