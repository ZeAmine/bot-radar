"""Connecteur direct léger pour Momox Shop FR."""

import logging

import httpx

from app.core.constants import MatchConfidence
from app.core.http_client import get_shared_http_client
from app.providers.base import CollectionAttempt, MarketOffer, MarketOffers, failed_offers
from app.providers.headers import COMMON_BROWSER_HEADERS

logger = logging.getLogger(__name__)


class MomoxProvider:
    """Connecteur direct léger pour interroger le catalogue Momox Shop France."""

    name: str = "momox"
    STANDARD_SHIPPING_CENTS: int = 299  # 2,99 € de port unitaire standard
    FREE_SHIPPING_THRESHOLD_CENTS: int = 1900  # Port gratuit à partir de 19 €

    def __init__(
        self,
        base_url: str = "https://api.momox-shop.fr/v1",
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
        """Recherche une offre d'occasion sur Momox par ISBN-13 en priorité."""
        _ = (title, author, edition)
        if not isbn13 or len(isbn13.strip()) < 10:
            return MarketOffers(attempts=(CollectionAttempt(self.name, status="skipped"),))

        clean_isbn = isbn13.strip()
        search_url = f"{self.base_url}/products"
        params: dict[str, str | int] = {"filter[searchTerm]": clean_isbn, "page[limit]": 1}

        try:
            client = self.http_client or get_shared_http_client()
            response = await client.get(
                search_url,
                params=params,
                headers=COMMON_BROWSER_HEADERS,
                timeout=5.0,
            )
            if response.status_code != 200:
                return failed_offers(self.name, response=response)

            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Réponse invalide")
            if not isinstance(data.get("data"), list):
                raise ValueError("Liste de produits absente")
            products = data.get("data", [])
            if not products:
                return []

            product = products[0]
            attrs = product.get("attributes", {})
            price_float = attrs.get("price", 0.0)
            item_price_cents = round(float(price_float) * 100)

            if item_price_cents <= 0:
                return []

            shipping_price_cents = (
                0
                if item_price_cents >= self.FREE_SHIPPING_THRESHOLD_CENTS
                else self.STANDARD_SHIPPING_CENTS
            )
            delivered_price_cents = item_price_cents + shipping_price_cents

            slug = attrs.get("slug", clean_isbn)
            source_url = f"https://www.momox-shop.fr/produit/{slug}"

            return [
                MarketOffer(
                    provider_name=self.name,
                    item_price_cents=item_price_cents,
                    shipping_price_cents=shipping_price_cents,
                    delivered_price_cents=delivered_price_cents,
                    source_url=source_url,
                    currency="EUR",
                    item_condition=attrs.get("condition", "Occasion"),
                    confidence=MatchConfidence.HIGH,
                    is_available=True,
                    is_comparable=True,
                    raw_payload=product,
                )
            ]
        except Exception as exc:
            logger.debug("Collecte momox échouée : %s", type(exc).__name__)
            return failed_offers(self.name, error=exc)
