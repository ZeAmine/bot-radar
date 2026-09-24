"""Registre des fournisseurs de marché et agrégation concurrente contrôlée."""

import asyncio
import logging

import httpx

from app.providers.base import BookMarketProvider, MarketOffer
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
from app.providers.ebay_browse import EbayBrowseProvider
from app.providers.gibert import GibertProvider
from app.providers.momox import MomoxProvider
from app.providers.recyclivre import RecyclivreProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Registre centralisant les fournisseurs de prix et coordonnant leur interrogation."""

    def __init__(
        self,
        providers: list[BookMarketProvider] | None = None,
        max_concurrency: int = 5,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if providers is not None:
            self.providers = providers
        else:
            # Providers activés par défaut : Chasse-aux-livres en priorité (agrégateur)
            default_providers: list[BookMarketProvider] = [
                ChasseAuxLivresProvider(http_client=http_client),
                MomoxProvider(http_client=http_client),
                RecyclivreProvider(http_client=http_client),
                GibertProvider(http_client=http_client),
            ]
            if EbayBrowseProvider.is_configured():
                default_providers.append(EbayBrowseProvider(http_client=http_client))
            self.providers = default_providers
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def fetch_offers_from_provider(
        self,
        provider: BookMarketProvider,
        *,
        isbn13: str | None,
        title: str,
        author: str | None = None,
        edition: str | None = None,
    ) -> list[MarketOffer]:
        """Interroge un unique fournisseur sous protection de sémaphore et timeout."""
        async with self._semaphore:
            try:
                return await asyncio.wait_for(
                    provider.get_offers(
                        isbn13=isbn13,
                        title=title,
                        author=author,
                        edition=edition,
                    ),
                    timeout=12.0,
                )
            except Exception as exc:
                logger.debug(
                    "Erreur lors de l'appel au provider '%s': %s",
                    provider.name,
                    exc,
                )
                return []

    async def fetch_all_offers(
        self,
        *,
        isbn13: str | None,
        title: str,
        author: str | None = None,
        edition: str | None = None,
    ) -> list[MarketOffer]:
        """Interroge l'ensemble des fournisseurs enregistrés en parallèle contrôlé."""
        tasks = [
            self.fetch_offers_from_provider(
                p,
                isbn13=isbn13,
                title=title,
                author=author,
                edition=edition,
            )
            for p in self.providers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_offers: list[MarketOffer] = []
        for res in results:
            if isinstance(res, list):
                all_offers.extend(res)

        return all_offers

    @staticmethod
    def select_best_offer(offers: list[MarketOffer]) -> MarketOffer | None:
        """Sélectionne l'offre comparable au prix total livré le plus bas du marché."""
        comparable_offers = [
            o
            for o in offers
            if o.is_comparable
            and o.is_available
            and o.delivered_price_cents > 0
            and (o.shipping_price_cents is not None or o.price_includes_shipping)
        ]

        if not comparable_offers:
            return None

        # Tri croissant par delivered_price_cents
        comparable_offers.sort(key=lambda o: o.delivered_price_cents)
        return comparable_offers[0]
