"""Connecteur direct léger pour Gibert."""

import logging

import httpx

from app.core.constants import MatchConfidence
from app.core.http_client import get_shared_http_client
from app.providers.base import MarketOffer, MarketOffers
from app.providers.headers import COMMON_BROWSER_HEADERS

logger = logging.getLogger(__name__)


class GibertProvider:
    """Connecteur direct léger pour interroger le catalogue Gibert (occasion uniquement)."""

    name: str = "gibert"
    STANDARD_SHIPPING_CENTS: int = 350  # Frais de livraison standard Gibert (3,50 €)

    def __init__(
        self,
        base_url: str = "https://www.gibert.com/api",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url
        self.http_client = http_client

    async def get_offers(
        self,
        *,
        isbn13: str | None,
        title: str,
        author: str | None = None,
        edition: str | None = None,
    ) -> list[MarketOffer]:
        """Recherche une offre d'occasion sur Gibert par ISBN-13 (exclut le neuf)."""
        _ = (title, author, edition)
        if not isbn13 or len(isbn13.strip()) < 10:
            return []

        clean_isbn = isbn13.strip()
        search_url = f"{self.base_url}/catalog/search"
        params = {"q": clean_isbn}

        try:
            client = self.http_client or get_shared_http_client()
            response = await client.get(
                search_url,
                params=params,
                headers=COMMON_BROWSER_HEADERS,
                timeout=5.0,
            )
            if response.status_code != 200:
                return MarketOffers(failed_providers=(self.name,))

            data = response.json()
            items = data.get("products", data.get("items", []))
            if not items:
                return []

            product = items[0]
            # Distinction stricte : retenir l'offre occasion en stock
            used_price_val = product.get("used_price", product.get("occasion_price"))
            if not used_price_val:
                # Si non séparé, vérifier le champ condition
                if product.get("is_used") or product.get("condition", "").lower() in (
                    "occasion",
                    "used",
                ):
                    used_price_val = product.get("price")
                else:
                    # Produit neuf uniquement : rejet
                    return []

            item_price_cents = round(float(used_price_val) * 100)
            if item_price_cents <= 0:
                return []

            delivered_price_cents = item_price_cents + self.STANDARD_SHIPPING_CENTS
            source_url = (
                product.get("url") or f"https://www.gibert.com/livres/{clean_isbn}.html"
            )

            return [
                MarketOffer(
                    provider_name=self.name,
                    item_price_cents=item_price_cents,
                    shipping_price_cents=self.STANDARD_SHIPPING_CENTS,
                    delivered_price_cents=delivered_price_cents,
                    source_url=source_url,
                    currency="EUR",
                    item_condition="Occasion",
                    confidence=MatchConfidence.HIGH,
                    is_available=True,
                    is_comparable=True,
                    raw_payload=product,
                )
            ]
        except Exception as exc:
            logger.debug("Erreur silencieuse connecteur Gibert: %s", exc)
            return MarketOffers(failed_providers=(self.name,))
