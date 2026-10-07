"""Connecteur direct léger pour Recyclivre."""

import logging

import httpx

from app.core.constants import MatchConfidence
from app.core.http_client import get_shared_http_client
from app.providers.base import MarketOffer, MarketOffers
from app.providers.headers import COMMON_BROWSER_HEADERS

logger = logging.getLogger(__name__)


class RecyclivreProvider:
    """Connecteur direct léger pour interroger le catalogue Recyclivre."""

    name: str = "recyclivre"
    STANDARD_SHIPPING_CENTS: int = 299  # Frais de port standard unitaire (2,99 €)

    def __init__(
        self,
        base_url: str = "https://www.recyclivre.com/api/v1",
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
        """Recherche une offre d'occasion sur Recyclivre par ISBN-13 / EAN."""
        _ = (title, author, edition)
        if not isbn13 or len(isbn13.strip()) < 10:
            return []

        clean_isbn = isbn13.strip()
        search_url = f"{self.base_url}/products/search"
        params: dict[str, str | int] = {"query": clean_isbn, "hitsPerPage": 1}

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
            items = data.get("hits", data.get("products", []))
            if not items:
                return []

            item = items[0]
            price_val = item.get("price", item.get("unit_price", 0.0))
            item_price_cents = round(float(price_val) * 100)

            if item_price_cents <= 0:
                return []

            delivered_price_cents = item_price_cents + self.STANDARD_SHIPPING_CENTS
            source_url = item.get("url") or f"https://www.recyclivre.com/shop/{clean_isbn}"

            return [
                MarketOffer(
                    provider_name=self.name,
                    item_price_cents=item_price_cents,
                    shipping_price_cents=self.STANDARD_SHIPPING_CENTS,
                    delivered_price_cents=delivered_price_cents,
                    source_url=source_url,
                    currency="EUR",
                    item_condition=item.get("condition", "Occasion"),
                    confidence=MatchConfidence.HIGH,
                    is_available=True,
                    is_comparable=True,
                    raw_payload=item,
                )
            ]
        except Exception as exc:
            logger.debug("Erreur silencieuse connecteur Recyclivre: %s", exc)
            return MarketOffers(failed_providers=(self.name,))
