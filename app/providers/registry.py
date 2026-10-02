"""Registre des fournisseurs de marché et agrégation concurrente contrôlée."""

import asyncio
import logging

import httpx

from app.domain.rules.market_offer import is_comparable_secondhand_offer
from app.providers.base import BookMarketProvider, MarketOffer, normalize_search_isbn
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
from app.providers.ebay_browse import EbayBrowseProvider
from app.providers.gibert import GibertProvider
from app.providers.momox import MomoxProvider
from app.providers.recyclivre import RecyclivreProvider

logger = logging.getLogger(__name__)

PROVIDER_TIMEOUT_SECONDS: float = 12.0
DEFAULT_MAX_CONCURRENCY: int = 5


class ProviderRegistry:
    """Registre centralisant les fournisseurs de prix et coordonnant leur interrogation."""

    def __init__(
        self,
        providers: list[BookMarketProvider] | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if providers is not None:
            self.providers = providers
        else:
            # Chasse-aux-livres est un agrégateur multi-boutiques : il est interrogé en
            # premier et ses résultats portent déjà l'historique 1 an. Les connecteurs
            # directs ne servent que de filet quand l'agrégateur ne répond pas.
            # eBay n'est ajouté que si ses identifiants sont configurés.
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
                    timeout=PROVIDER_TIMEOUT_SECONDS,
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
        """Interroge l'ensemble des fournisseurs enregistrés en parallèle contrôlé.

        L'ISBN n'est transmis qu'après validation de sa clé de contrôle : une
        requête par ISBN erroné renverrait l'ouvrage d'un autre livre et sa cote
        serait stockée sous l'identifiant du livre attendu.
        """
        clean_isbn = normalize_search_isbn(isbn13)
        tasks = [
            self.fetch_offers_from_provider(
                p,
                isbn13=clean_isbn,
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
    def _merchant_key(offer: MarketOffer) -> str:
        """Identifie la boutique derrière une offre (l'agrégateur peut la nommer)."""
        if offer.raw_payload:
            merchant = offer.raw_payload.get("best_merchant")
            if isinstance(merchant, str) and merchant.strip():
                return merchant.strip().lower()
        return offer.provider_name.strip().lower()

    @staticmethod
    def select_best_offer(offers: list[MarketOffer]) -> MarketOffer | None:
        """Sélectionne l'offre d'occasion comparable au prix total livré le plus bas.

        Seules les offres d'occasion dont le port est connu sont retenues : le prix
        affiché doit être celui réellement payé, frais de livraison inclus.
        """
        comparable_offers = [
            o
            for o in offers
            if is_comparable_secondhand_offer(o)
            and o.is_available
            and o.delivered_price_cents > 0
            and (o.shipping_price_cents is not None or o.price_includes_shipping)
        ]

        if not comparable_offers:
            return None

        # Un même marchand peut être remonté par l'agrégateur et par son connecteur
        # direct : on ne garde que son offre la moins chère avant le tri final.
        cheapest_by_merchant: dict[str, MarketOffer] = {}
        for offer in comparable_offers:
            key = ProviderRegistry._merchant_key(offer)
            best = cheapest_by_merchant.get(key)
            if best is None or offer.delivered_price_cents < best.delivered_price_cents:
                cheapest_by_merchant[key] = offer

        # Tri croissant par delivered_price_cents
        return min(cheapest_by_merchant.values(), key=lambda o: o.delivered_price_cents)
