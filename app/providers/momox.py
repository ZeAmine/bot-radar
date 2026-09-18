"""Connecteur direct léger pour Momox Shop FR."""

import logging

import httpx

from app.core.constants import MatchConfidence
from app.providers.base import MarketOffer
from app.providers.headers import COMMON_BROWSER_HEADERS

logger = logging.getLogger(__name__)


class MomoxProvider:
    """Connecteur direct léger pour interroger le catalogue Momox Shop France."""

    name: str = "momox"
    STANDARD_SHIPPING_CENTS: int = 299  # 2,99 € de port unitaire standard
    FREE_SHIPPING_THRESHOLD_CENTS: int = 1900  # Port gratuit à partir de 19 €

    def __init__(self, base_url: str = "https://api.momox-shop.fr/v1") -> None:
        self.base_url = base_url

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
            return []

        clean_isbn = isbn13.strip()
        search_url = f"{self.base_url}/products"
        params: dict[str, str | int] = {"filter[searchTerm]": clean_isbn, "page[limit]": 1}

        try:
            async with httpx.AsyncClient(headers=COMMON_BROWSER_HEADERS, timeout=5.0) as client:
                response = await client.get(search_url, params=params)
                if response.status_code != 200:
                    return []

                data = response.json()
                products = data.get("data", [])
                if not products:
                    return []

                product = products[0]
                attrs = product.get("attributes", {})
                price_float = attrs.get("price", 0.0)
                item_price_cents = round(float(price_float) * 100)

                if item_price_cents <= 0:
                    return []

                # Application des règles de livraison Momox
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
            logger.debug("Erreur silencieuse connecteur Momox: %s", exc)
            return []
