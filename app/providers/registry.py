"""Registre des fournisseurs de marché et agrégation concurrente contrôlée."""

import asyncio
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic

import httpx

from app.domain.rules.market_offer import is_comparable_secondhand_offer
from app.providers.base import (
    BookMarketProvider,
    CollectionAttempt,
    MarketOffer,
    MarketOffers,
    failed_offers,
    normalize_search_isbn,
    requested_components,
)
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider

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
        single_attempt: bool = False,
    ) -> None:
        if providers is not None:
            self.providers = providers
        else:
            # Chasse-aux-livres est la seule source de cote : elle porte les offres
            # courantes et l'historique 1 an, sans réglage ni identifiant à fournir.
            self.providers = [
                ChasseAuxLivresProvider(http_client=http_client,
                                       max_retries=0 if single_attempt else 2),
            ]
        self.failed_provider_names: set[str] = set()
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def fetch_offers_from_provider(
        self,
        provider: BookMarketProvider,
        *,
        isbn13: str | None,
        title: str,
        author: str | None = None,
        edition: str | None = None,
        source_url: str | None = None,
    ) -> list[MarketOffer]:
        """Interroge un unique fournisseur sous protection de sémaphore et timeout."""
        if "current" not in requested_components.get() and not isinstance(
            provider, ChasseAuxLivresProvider,
        ):
            return MarketOffers()
        async with self._semaphore:
            started = monotonic()
            try:
                if isinstance(provider, ChasseAuxLivresProvider) and source_url:
                    operation = provider.get_offers(
                        isbn13=isbn13, title=title, author=author, edition=edition,
                        source_url=source_url,
                    )
                else:
                    operation = provider.get_offers(
                        isbn13=isbn13,
                        title=title,
                        author=author,
                        edition=edition,
                    )
                offers = await asyncio.wait_for(
                    operation,
                    timeout=(18.0 if isinstance(provider, ChasseAuxLivresProvider)
                             else PROVIDER_TIMEOUT_SECONDS),
                )
                observed_at = datetime.now(UTC)
                for offer in offers or []:
                    if offer.observed_at is None and not offer.is_historical:
                        offer.observed_at = observed_at
                    if offer.observation_id is None:
                        offer.observation_id = uuid.uuid4()
                attempts = getattr(offers, "attempts", ()) or (CollectionAttempt(
                    provider.name,
                    status="failed" if getattr(offers, "failed_providers", ()) else "success",
                    reason="unclassified" if getattr(offers, "failed_providers", ()) else None,
                ),)
                return MarketOffers(
                    offers or [], failed_providers=getattr(offers, "failed_providers", ()),
                    historical_checked=getattr(offers, "historical_checked", False),
                    attempts=tuple(replace(a, duration_seconds=monotonic() - started)
                                   for a in attempts),
                    emitted_requests=getattr(offers, "emitted_requests", 0),
                )
            except Exception as exc:
                failed = failed_offers(
                    provider.name, error=exc,
                    operation="current" if "current" in requested_components.get() else "history",
                )
                failed.attempts = tuple(replace(a, duration_seconds=monotonic() - started)
                                        for a in failed.attempts)
                return failed

    async def fetch_all_offers(
        self,
        *,
        isbn13: str | None,
        title: str,
        author: str | None = None,
        edition: str | None = None,
        source_url: str | None = None,
    ) -> MarketOffers:
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
                source_url=source_url,
            )
            for p in self.providers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_offers: list[MarketOffer] = []
        failures: set[str] = set()
        attempts: list[CollectionAttempt] = []
        for provider, res in zip(self.providers, results, strict=True):
            if isinstance(res, BaseException):
                failures.add(provider.name)
                attempts.append(CollectionAttempt(
                    provider.name, status="failed",
                    reason=("cancelled" if isinstance(res, asyncio.CancelledError)
                            else "internal_error"),
                ))
            if isinstance(res, list):
                all_offers.extend(res)
                attempts.extend(getattr(res, "attempts", ()))
                failures.update(getattr(res, "failed_providers", ()))

        self.failed_provider_names.update(failures)
        return MarketOffers(
            all_offers, failed_providers=tuple(sorted(failures)),
            attempts=tuple(attempts),
            historical_checked=any(getattr(res, "historical_checked", False) for res in results),
            emitted_requests=sum(
                getattr(res, "emitted_requests", 0) for res in results
                if isinstance(res, list)
            ),
        )

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
            and not o.is_historical
            and o.delivered_price_cents > 0
            and (o.shipping_price_cents is not None or o.price_includes_shipping)
        ]

        if not comparable_offers:
            return None

        # Un marchand peut apparaître plusieurs fois : le minimum sur les prix est
        # le même que le minimum des minima par marchand, sans regroupement à faire.
        return min(comparable_offers, key=lambda o: o.delivered_price_cents)
