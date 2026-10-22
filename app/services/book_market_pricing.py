"""Cotation partagée et persistance des prix par composante."""

import asyncio
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from app.core import alert_latency
from app.core.alert_latency import observe_background
from app.core.constants import ComparisonStatus, MatchConfidence
from app.core.tasks import safe_create_task
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.db.session import AsyncSessionLocal
from app.domain.rules.market_offer import is_comparable_secondhand_offer
from app.providers.base import (
    CollectionAttempt,
    MarketOffer,
    MarketOffers,
    extract_best_historical_low,
    normalize_search_isbn,
    requested_components,
)
from app.providers.registry import ProviderRegistry
from app.services.administrative_deferral import (
    administrative_attempts,
    administrative_deferral_only,
    record_administrative_deferral,
)
from app.services.market_price_service import series_reference_total
from app.services.market_refresh_policy import (
    aware,
    collection_due,
    component_due,
    record_component,
    reservation_lock,
)
from app.utils.price import format_cents

logger = logging.getLogger(__name__)

# Durée maximale d'interrogation d'un livre, tous fournisseurs compris.
BOOK_FETCH_TIMEOUT_SECONDS = 20.0
# Durée de validité du cache selon la qualité de la donnée collectée.
CACHE_HOURS_WITH_PRICE = 168
CACHE_HOURS_WITHOUT_PRICE = 6

# Livres dont la cotation est déjà en vol : un seul passage à la fois par livre.
_pricing_in_flight: set[uuid.UUID] = set()
_pricing_lock = threading.Lock()


@dataclass(frozen=True)
class BookPricingResult:
    """Issue du calcul de cote d'un livre, indépendante de toute persistance."""

    offers: list[MarketOffer]
    lowest_delivered_price_cents: int | None
    best_provider_name: str | None
    best_source_url: str | None
    confidence: MatchConfidence | None
    comparison_status: ComparisonStatus
    cache_duration_hours: int
    historical_low_1y_cents: int | None
    failed_providers: tuple[str, ...] = ()
    attempts: tuple[CollectionAttempt, ...] = ()
    current_observed_at: datetime | None = None
    historical_checked: bool = False
    price_confirmed: bool = False
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    emitted_requests: int = 0

    @property
    def has_data(self) -> bool:
        """Vrai si cette passe a réellement trouvé de quoi coter le livre."""
        return bool(
            self.lowest_delivered_price_cents is not None
            or self.historical_low_1y_cents is not None
        )

    @property
    def current_collection_complete(self) -> bool:
        current = [a for a in self.attempts if a.operation == "current" and a.status != "skipped"]
        if self.attempts:
            return bool(current) and all(a.status == "success" for a in current)
        return not self.failed_providers and (
            self.lowest_delivered_price_cents is None or self.price_confirmed
        )

    @property
    def current_collection_succeeded(self) -> bool:
        return self.current_collection_complete

    @property
    def current_collection_usable(self) -> bool:
        if not self.attempts:
            return not self.failed_providers and (
                self.lowest_delivered_price_cents is None or self.price_confirmed
            )
        current = [a for a in self.attempts if a.operation == "current" and a.status != "skipped"]
        if not current or any(a.status == "failed" for a in current):
            return False
        if self.lowest_delivered_price_cents is not None and self.price_confirmed:
            return True
        return all(a.status == "success" for a in current)

    @property
    def current_collection_reason(self) -> str:
        current = [a for a in self.attempts if a.operation == "current" and a.status != "skipped"]
        if not self.attempts:
            if self.failed_providers:
                return "failed"
            if self.lowest_delivered_price_cents is None:
                return "empty_complete"
            return "confirmed_price" if self.price_confirmed else "unproven_price"
        if not current:
            return "failed" if self.failed_providers else "no_current_attempt"
        if any(a.status == "failed" for a in current):
            return "failed"
        if all(a.status == "success" for a in current):
            if self.lowest_delivered_price_cents is None:
                return "empty_complete"
            return "confirmed_price" if self.price_confirmed else "all_sources_succeeded"
        if self.lowest_delivered_price_cents is None:
            return "partial_without_price"
        return "confirmed_price" if self.price_confirmed else "unproven_price"

    @staticmethod
    def _price_is_confirmed(
        offers: list[MarketOffer], best_offer: MarketOffer | None,
    ) -> bool:
        if best_offer is None:
            return False
        return any(
            attempt.operation == "current"
            and attempt.provider == best_offer.provider_name
            and attempt.status == "success"
            for attempt in getattr(offers, "attempts", ())
        )

    @staticmethod
    def from_offers(
        offers: list[MarketOffer],
        best_offer: MarketOffer | None,
        historical_low_cents: int | None,
    ) -> "BookPricingResult":
        """Déduit l'état du cache à partir des offres collectées.

        Trois issues possibles : une offre d'occasion disponible, un historique
        certifié mais plus rien en stock, ou aucune donnée exploitable.
        """
        unconfirmed_sources = {a.provider for a in getattr(offers, "attempts", ())
                           if a.operation == "current" and a.status in {"partial", "failed"}}
        if best_offer is not None and best_offer.provider_name in unconfirmed_sources:
            best_offer = ProviderRegistry.select_best_offer([
                offer for offer in offers if offer.provider_name not in unconfirmed_sources
            ])
        price_confirmed = BookPricingResult._price_is_confirmed(offers, best_offer)
        if best_offer is not None and not price_confirmed:
            best_offer = None
        if best_offer is not None:
            merchant = None
            if best_offer.raw_payload:
                raw_merchant = best_offer.raw_payload.get("best_merchant")
                merchant = raw_merchant if isinstance(raw_merchant, str) and raw_merchant else None
            return BookPricingResult(
                offers=offers,
                lowest_delivered_price_cents=best_offer.delivered_price_cents,
                best_provider_name=merchant or best_offer.provider_name,
                best_source_url=best_offer.source_url,
                confidence=best_offer.confidence,
                comparison_status=ComparisonStatus.AVAILABLE,
                cache_duration_hours=CACHE_HOURS_WITH_PRICE,
                historical_low_1y_cents=historical_low_cents,
                failed_providers=getattr(offers, "failed_providers", ()),
                attempts=getattr(offers, "attempts", ()) or tuple(
                    CollectionAttempt(name, status="failed", reason="unclassified")
                    for name in getattr(offers, "failed_providers", ())
                ),
                emitted_requests=getattr(offers, "emitted_requests", 0),
                historical_checked=getattr(offers, "historical_checked", False),
                price_confirmed=price_confirmed,
                current_observed_at=best_offer.observed_at,
            )

        if historical_low_cents is not None:
            return BookPricingResult(
                offers=offers,
                lowest_delivered_price_cents=None,
                best_provider_name=None,
                best_source_url=None,
                confidence=None,
                comparison_status=ComparisonStatus.UNAVAILABLE,
                cache_duration_hours=CACHE_HOURS_WITH_PRICE,
                historical_low_1y_cents=historical_low_cents,
                failed_providers=getattr(offers, "failed_providers", ()),
                attempts=getattr(offers, "attempts", ()) or tuple(
                    CollectionAttempt(name, status="failed", reason="unclassified")
                    for name in getattr(offers, "failed_providers", ())
                ),
                emitted_requests=getattr(offers, "emitted_requests", 0),
                historical_checked=getattr(offers, "historical_checked", False),
            )

        return BookPricingResult(
            offers=offers,
            lowest_delivered_price_cents=None,
            best_provider_name=None,
            best_source_url=None,
            confidence=None,
            comparison_status=ComparisonStatus.UNAVAILABLE,
            cache_duration_hours=CACHE_HOURS_WITHOUT_PRICE,
            historical_low_1y_cents=None,
            failed_providers=getattr(offers, "failed_providers", ()),
            attempts=getattr(offers, "attempts", ()) or tuple(
                CollectionAttempt(name, status="failed", reason="unclassified")
                for name in getattr(offers, "failed_providers", ())
            ),
            emitted_requests=getattr(offers, "emitted_requests", 0),
            historical_checked=getattr(offers, "historical_checked", False),
        )


def _current_component_requested(result: BookPricingResult) -> bool:
    return not result.attempts or any(
        attempt.operation == "current" and attempt.status != "skipped"
        for attempt in result.attempts
    )


def _history_only(result: BookPricingResult) -> bool:
    return any(
        attempt.operation == "history" for attempt in result.attempts
    ) and not any(
        attempt.operation == "current" and attempt.status != "skipped"
        for attempt in result.attempts
    )


def pricing_stat(cache: BookMarketCache | None) -> str:
    """Ventilation d'une passe de cotation, lue dans la cote consolidée."""
    if cache is not None and cache.collection_status == "failed":
        return "errors"
    if cache is not None and cache.collection_status == "empty":
        return "unavailable"
    if cache is not None and cache.comparison_status == ComparisonStatus.AVAILABLE:
        return "available"
    return "unavailable"


async def fetch_offers_for_book(
    book: Book,
    registry: ProviderRegistry | None = None,
    *, retry_transient: bool = False,
) -> list[MarketOffer]:
    """Interroge tous les fournisseurs pour un livre, ISBN valide et titre ensemble.

    Le titre accompagne toujours l'ISBN : un agrégateur qui ne trouve rien par
    code sait retrouver l'ouvrage par son titre, et c'était précisément ce repli
    qui manquait à la création d'une alerte — d'où une cote vide en base, donc
    une alerte partie sans prix alors que le bouton, lui, trouvait la cote.
    """
    active_registry = registry or (ProviderRegistry(single_attempt=True)
                                   if retry_transient else ProviderRegistry())
    now = datetime.now(UTC)
    cache = book.__dict__.get("market_cache")
    components = frozenset(component for component in ("current", "history")
                           if component_due(cache, component, now) <= now)
    from app.providers.chasse_aux_livres import ChasseAuxLivresProvider, verified_product_url

    history = cache.source_history if cache and isinstance(cache.source_history, dict) else {}
    source_url = verified_product_url(history.get("source_url"), book.isbn13)
    if not source_url and cache and cache.best_provider_name == "chasse_aux_livres":
        source_url = verified_product_url(cache.best_source_url, book.isbn13)
    source_options = {"source_url": source_url} if source_url else {}
    token = requested_components.set(components)
    try:
        offers = await asyncio.wait_for(
            active_registry.fetch_all_offers(
                isbn13=normalize_search_isbn(book.isbn13),
                title=book.title,
                author=book.author,
                edition=book.edition,
                **source_options,
            ),
            timeout=BOOK_FETCH_TIMEOUT_SECONDS,
        )
        if retry_transient:
            retryable = [a for a in getattr(offers, "attempts", ())
                         if a.status == "failed" and a.operation == "current"
                         and (a.retry_after_seconds is None or a.retry_after_seconds <= 3)
                         and (a.reason in {"timeout", "connection", "empty_response"}
                              or (a.http_status is not None and a.http_status >= 500)
                              or (a.http_status == 429 and a.retry_after_seconds is not None
                                  and a.retry_after_seconds <= 3))]
            names = {a.provider for a in retryable}
            empty_sources = {a.provider for a in retryable if a.reason == "empty_response"}
            providers = [p for p in active_registry.providers if p.name in names and (
                p.name not in empty_sources or not isinstance(p, ChasseAuxLivresProvider)
                or p.max_retries == 0
            )]
            names = {p.name for p in providers}
            retryable = [a for a in retryable if a.provider in names]
            if providers:
                delay = max((a.retry_after_seconds or 0 for a in retryable), default=0)
                if any(p.name in empty_sources for p in providers):
                    delay = max(delay, 1.5)
                if delay:
                    await asyncio.sleep(delay)
                retry_registry = ProviderRegistry(providers=providers)
                for attempt in retryable:
                    logger.warning("Reprise de collecte transitoire", extra={
                        "book_id": str(book.id), "provider": attempt.provider,
                        "operation": attempt.operation, "reason": attempt.reason,
                        "http_status": attempt.http_status,
                    })
                retry_scope = requested_components.set(frozenset({"current"}))
                try:
                    retried = await asyncio.wait_for(retry_registry.fetch_all_offers(
                        isbn13=normalize_search_isbn(book.isbn13), title=book.title,
                        author=book.author, edition=book.edition, **source_options,
                    ), timeout=BOOK_FETCH_TIMEOUT_SECONDS)
                except TimeoutError:
                    retried = MarketOffers(
                        failed_providers=tuple(sorted(names)) + ("reprise marché",),
                        attempts=tuple(a for a in getattr(offers, "attempts", ())
                                       if a.provider in names) + (CollectionAttempt(
                                           "reprise marché", status="failed", reason="timeout",
                                       ),),
                    )
                finally:
                    requested_components.reset(retry_scope)
                retained_attempts = tuple(a for a in getattr(offers, "attempts", ())
                                          if a.provider not in names or a.operation != "current")
                retried_attempts = tuple(
                    a for a in retried.attempts
                    if not (a.operation == "history" and a.status == "skipped")
                )
                retained_failures = {
                    a.provider for a in retained_attempts if a.status in {"partial", "failed"}
                }
                offers = MarketOffers(
                    [o for o in offers if o.provider_name not in names or o.is_historical]
                    + list(retried),
                    failed_providers=tuple(sorted(
                        (set(getattr(offers, "failed_providers", ())) - names)
                        | set(retried.failed_providers) | retained_failures)),
                    attempts=retained_attempts + retried_attempts,
                    historical_checked=getattr(offers, "historical_checked", False)
                    or retried.historical_checked,
                    emitted_requests=(
                        getattr(offers, "emitted_requests", 0) + retried.emitted_requests
                    ),
                )
        observed_at = datetime.now(UTC)
        for offer in offers:
            if offer.observed_at is None and not offer.is_historical:
                offer.observed_at = observed_at
            if offer.observation_id is None:
                offer.observation_id = uuid.uuid4()
        for attempt in getattr(offers, "attempts", ()):
            logger.log(logging.WARNING if attempt.status == "failed" else logging.INFO,
                       "Collecte marché", extra={"book_id": str(book.id),
                       "provider": attempt.provider, "operation": attempt.operation,
                       "outcome": attempt.status, "reason": attempt.reason,
                       "http_status": attempt.http_status,
                       "duration_seconds": attempt.duration_seconds, "stage": attempt.stage,
                       "response_content_type": attempt.response_content_type,
                       "response_bytes": attempt.response_bytes,
                       "engines_requested": attempt.engines_requested,
                       "engines_received": attempt.engines_received,
                       "poll_count": attempt.poll_count,
                       "lookup_seconds": attempt.lookup_seconds})
        return offers
    except TimeoutError:
        logger.warning("Délai global de collecte dépassé pour %s", book.id)
        return MarketOffers(failed_providers=("collecte marché",), attempts=(
            CollectionAttempt("collecte marché", status="failed", reason="timeout"),
        ))
    finally:
        requested_components.reset(token)


async def _refresh_series_totals(session: AsyncSession, book_id: uuid.UUID) -> None:
    """Réécrit le total des séries qui portent ce livre — après chaque écriture de cote.

    C'est la **seule** adresse où le total est écrit (invariant : une seule
    écriture, un seul chemin). Une alerte sans ISBN mais à un seul livre relié
    n'est pas une série : son total reste ``NULL``, l'écran n'a rien à sommer.
    """
    # Autoflush désactivé en production : sans ce flush, la relecture avec
    # populate_existing écraserait les métadonnées de collecte en mémoire.
    await session.flush()
    repo = MarketPriceRepository(session)
    alert_ids = await repo.get_alert_ids_linked_to_book(book_id)
    if not alert_ids:
        return
    for alert_id in sorted(alert_ids):
        await refresh_series_total(session, alert_id)


async def refresh_series_total(session: AsyncSession, alert_id: uuid.UUID) -> None:
    """Calcul central sur les liaisons/caches relus après acquisition du verrou."""
    from app.core.constants import AlertStatus
    from app.db.repositories.alert_repo import AlertRepository

    await session.flush()
    repo = MarketPriceRepository(session)
    await repo.lock_series(alert_id)
    current = await AlertRepository(session).get_by_id(alert_id, refresh=True)
    if current is None or current.status == AlertStatus.DELETED:
        return
    books = await BookRepository(session).get_all_books_for_alert(alert_id)
    await repo.set_series_total(alert_id, series_reference_total(books) if len(books) > 1 else None)


async def recalculate_invalidated_series(session: AsyncSession) -> None:
    """Draine les seules mutations de la transaction par la même adresse de calcul."""
    for alert_id in sorted(BookRepository(session).take_invalidated_series_ids()):
        await refresh_series_total(session, alert_id)


def _release_price_reservation(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        lock = session.info.pop("price_reservation_lock", None)
        if lock is not None:
            lock.release()


async def _reserve_next_refresh(
    session: AsyncSession, book_id: uuid.UUID, cache: BookMarketCache,
) -> None:
    if cache.collection_status == "deferred" and cache.next_refresh_at is not None:
        return
    if "price_reservation_lock" not in session.info:
        if not event.contains(
            session.sync_session, "after_transaction_end", _release_price_reservation,
        ):
            event.listen(session.sync_session, "after_transaction_end", _release_price_reservation)
        lock = reservation_lock()
        await lock.acquire()
        session.info["price_reservation_lock"] = lock
    await BookRepository(session).plan_price_refreshes(book_ids={book_id})


async def persist_market_result(
    session: AsyncSession,
    book_id: uuid.UUID,
    result: BookPricingResult,
) -> BookMarketCache:
    """Enregistre snapshots et cache consolidé pour un livre, et rend la cote écrite.

    Chaque sortie de cette fonction réécrit le total des séries du livre : le
    total affiché n'est jamais recomposé à l'affichage, il suit les cotes parce
    qu'il est réécrit là où elles le sont.
    """
    repo = MarketPriceRepository(session)
    for offer in result.offers:
        await repo.save_snapshot(book_id, offer)

    if _history_only(result):
        cache = await repo.get_cache_by_book_id(book_id)
        if cache is None:
            cache = BookMarketCache(book_id=book_id)
            session.add(cache)
            await session.flush()
        deferred_until = cache.next_refresh_at if cache.collection_status == "deferred" else None
        _stamp_collection(cache, result)
        if deferred_until is not None and aware(deferred_until) > datetime.now(UTC):
            cache.next_refresh_at = deferred_until
        await repo.refresh_annual_references([book_id], force=True)
        await _refresh_series_totals(session, book_id)
        await _reserve_next_refresh(session, book_id, cache)
        return cache

    if result.lowest_delivered_price_cents is None:
        # Résilience : un silence réseau ne doit pas priver l'utilisateur
        # d'un prix déjà connu et encore frais.
        existing = await repo.get_cache_by_book_id(book_id)
        if existing is not None and (
            result.failed_providers
            or (result.attempts and all(a.status == "skipped" for a in result.attempts))
            or (
                existing.comparison_status == ComparisonStatus.AVAILABLE
                and existing.lowest_delivered_price_cents is not None
            )
        ):
            logger.info(
                "Maintien résilient du prix en cache pour le livre ID %s (%s cents).",
                book_id,
                existing.lowest_delivered_price_cents,
            )
            await repo.refresh_annual_references([book_id], force=True)
            _stamp_collection(existing, result)
            # Conserver le relevé informatif, jamais renouveler sa validité sans observation.
            await _refresh_series_totals(session, book_id)
            await _reserve_next_refresh(session, book_id, existing)
            return existing

    cache = await repo.update_or_create_cache(
        book_id=book_id,
        lowest_delivered_price_cents=result.lowest_delivered_price_cents,
        best_provider_name=result.best_provider_name,
        best_source_url=result.best_source_url,
        confidence=result.confidence,
        comparison_status=result.comparison_status,
        cache_duration_hours=result.cache_duration_hours,
        historical_low_1y_cents=result.historical_low_1y_cents,
        observed_at=(result.current_observed_at if result.lowest_delivered_price_cents is not None
                     else result.collected_at if result.current_collection_succeeded else None),
    )
    await repo.refresh_annual_references([book_id], force=True)
    _stamp_collection(cache, result)
    await _refresh_series_totals(session, book_id)
    await _reserve_next_refresh(session, book_id, cache)
    return cache


def _stamp_collection(cache: BookMarketCache, result: BookPricingResult) -> None:
    """Conserve le résultat de tentative séparément de la date du dernier prix."""
    if administrative_deferral_only(result):
        retry_at = record_administrative_deferral(cache, result.attempts, result.collected_at)
        cache.next_refresh_at = retry_at
        logger.info(
            "Collecte CAL reportée avant toute requête pour le livre ID %s "
            "(raisons %s), reprise %s.",
            cache.book_id,
            ",".join(sorted({str(a.reason) for a in administrative_attempts(result.attempts)})),
            retry_at.isoformat(),
        )
        return
    current_requested = _current_component_requested(result)
    current_attempted = not _history_only(result)
    history_requested = not any(a.operation == "history" for a in result.attempts) or any(
        a.operation == "history" and a.status != "skipped" for a in result.attempts
    )
    if current_attempted:
        cache.last_attempt_at = result.collected_at
    if current_requested:
        logger.debug(
            "Décision de collecte pour le livre ID %s : %s.",
            cache.book_id,
            result.current_collection_reason,
        )
        if result.current_collection_usable:
            cache.last_successful_collection_at = result.collected_at
        record_component(cache, "current", succeeded=result.current_collection_usable,
                         now=result.collected_at)
    if result.historical_checked:
        cache.historical_checked_at = result.collected_at
        points = [offer for offer in result.offers
                  if offer.provider_name == "chasse_aux_livres" and offer.is_historical
                  and offer.item_condition == "Très bon état +" and offer.observed_at
                  and offer.price_includes_shipping and offer.delivered_price_cents > 0]
        if points:
            lowest = min(points, key=lambda offer: offer.delivered_price_cents)
            cache.historical_low_1y_cents = lowest.delivered_price_cents
            cache.historical_low_at = lowest.observed_at
            cache.source_history = {
                "price_cents": lowest.delivered_price_cents,
                "observed_at": lowest.observed_at.isoformat() if lowest.observed_at else None,
                "collected_at": result.collected_at.isoformat(),
                "source_url": lowest.source_url, "condition": "Très bon état +",
                "days": 365, "shipping_included": True,
            }
    if history_requested:
        record_component(cache, "history", succeeded=result.historical_checked,
                         now=result.collected_at)
    if not current_attempted:
        return
    cache.failed_provider_names = list(result.failed_providers)
    retained_offers = cache.current_offers
    unconfirmed_sources = {a.provider for a in result.attempts
                       if a.operation == "current" and a.status in {"partial", "failed"}}
    cache.current_offers = [
        {
            key: value
            for key, value in asdict(offer).items()
            if key not in {"observed_at", "observation_id"}
        }
        for offer in result.offers
        if offer.provider_name not in unconfirmed_sources
        and is_comparable_secondhand_offer(offer)
        and offer.is_available
        and not offer.is_historical
        and (offer.shipping_price_cents is not None or offer.price_includes_shipping)
    ]
    if not result.current_collection_usable and not result.price_confirmed:
        new_offers = cache.current_offers or []
        replaced = {str(offer.get("provider_name")) for offer in new_offers}
        cache.current_offers = [offer for offer in retained_offers or []
                                if str(offer.get("provider_name")) not in replaced] + new_offers
        merged = []
        required = {"provider_name", "item_price_cents", "shipping_price_cents",
                    "delivered_price_cents", "source_url"}
        for serialized in cache.current_offers:
            if required <= serialized.keys():
                merged.append(MarketOffer(**cast(dict[str, Any], serialized)))
        best = ProviderRegistry.select_best_offer(merged)
        if best is not None:
            cache.lowest_delivered_price_cents = best.delivered_price_cents
            merchant = (best.raw_payload or {}).get("best_merchant")
            cache.best_provider_name = merchant if isinstance(merchant, str) else best.provider_name
            cache.best_source_url = best.source_url
    cache.collection_status = (
        "skipped"
        if result.attempts and all(a.status == "skipped" for a in result.attempts)
        else "partial"
        if any(a.status == "partial" for a in result.attempts)
        or result.failed_providers and (result.has_data or result.current_collection_succeeded)
        else "failed"
        if result.failed_providers
        else "complete"
        if result.lowest_delivered_price_cents is not None
        else "empty"
    )


async def price_book(
    session: AsyncSession,
    book: Book,
    registry: ProviderRegistry | None = None,
    result_sink: list[BookPricingResult] | None = None,
    retry_transient: bool = False,
) -> BookMarketCache:
    """Cote un livre et écrit sa cote dans la session courante (sans commit)."""
    offers = await fetch_offers_for_book(
        book, registry=registry, **({"retry_transient": True} if retry_transient else {}),
    )
    result = BookPricingResult.from_offers(
        offers=offers,
        best_offer=ProviderRegistry.select_best_offer(offers),
        historical_low_cents=extract_best_historical_low(offers),
    )
    if result_sink is not None:
        result_sink.append(result)
    cache = await persist_market_result(session, book.id, result)
    logger.info(
        "Cote du livre ID %s (%s) : marché %s, minimum des observations conservées %s (%s).",
        book.id,
        book.title,
        format_cents(cache.lowest_delivered_price_cents)
        if cache.lowest_delivered_price_cents
        else "inconnu",
        format_cents(cache.annual_low_cents) if cache.annual_low_cents else "inconnu",
        pricing_stat(cache),
    )
    return cache


_pricing_tasks: dict[uuid.UUID, asyncio.Task[BookMarketCache | None]] = {}
_pricing_results: dict[uuid.UUID, list[BookPricingResult]] = {}


async def price_book_now(
    book_id: uuid.UUID,
    *,
    reason: str,
    registry: ProviderRegistry | None = None,
    result_sink: list[BookPricingResult] | None = None,
) -> BookMarketCache | None:
    """Partage une collecte déjà en vol pour le même livre."""
    task = _pricing_tasks.get(book_id)
    if task is None:
        results: list[BookPricingResult] = []
        task = safe_create_task(
            _price_book_once(book_id, reason=reason, registry=registry, result_sink=results),
            name=f"price_book_{book_id}",
        )
        _pricing_tasks[book_id] = task
        _pricing_results[book_id] = results

        def release(done: asyncio.Task[BookMarketCache | None]) -> None:
            if _pricing_tasks.get(book_id) is done:
                _pricing_tasks.pop(book_id, None)
                _pricing_results.pop(book_id, None)

        task.add_done_callback(release)
    results = _pricing_results[book_id]
    cache = await asyncio.shield(task)
    if result_sink is not None:
        result_sink.extend(results)
    return cache


async def _price_book_once(
    book_id: uuid.UUID,
    *,
    reason: str,
    registry: ProviderRegistry | None = None,
    result_sink: list[BookPricingResult] | None = None,
) -> BookMarketCache | None:
    """Cote un livre dans sa propre session. N'élève jamais.

    Un marchand muet n'a pas le droit de faire échouer l'écran qui a demandé la
    cote : l'appelant reçoit ``None`` et affiche ce qu'il sait déjà.
    """
    attempted_at: datetime | None = None
    try:
        async with AsyncSessionLocal() as session:
            book = await BookRepository(session).get_by_id(book_id)
            if book is None:
                logger.warning("Cotation ignorée : livre ID %s introuvable (%s).", book_id, reason)
                return None
            cache = await MarketPriceRepository(session).get_cache_by_book_id(book_id)
            if cache is not None and not collection_due(cache, datetime.now(UTC)):
                await _reserve_next_refresh(session, book_id, cache)
                await session.commit()
                return cache
            if cache is None:
                cache = BookMarketCache(book_id=book_id)
                session.add(cache)
            attempted_at = datetime.now(UTC)
            cache.last_attempt_at = attempted_at
            # L'essai est validé avant la collecte : il reste acquis si elle échoue.
            await session.commit()
            cache = await price_book(session, book, registry=registry, result_sink=result_sink,
                                     retry_transient=reason == "alert_created")
            await session.commit()
            return cache
    except Exception as exc:
        logger.warning(
            "Cotation du livre ID %s impossible (%s), erreur=%s",
            book_id, reason, type(exc).__name__,
        )
        if attempted_at is not None:
            try:
                async with AsyncSessionLocal() as recovery:
                    changed = await recovery.scalar(
                        update(BookMarketCache)
                        .where(
                            BookMarketCache.book_id == book_id,
                            BookMarketCache.last_attempt_at == attempted_at,
                        )
                        .values(collection_status="failed", next_refresh_at=None)
                        .returning(BookMarketCache.book_id)
                    )
                    if changed is not None:
                        cache = await MarketPriceRepository(recovery).get_cache_by_book_id(book_id)
                        if cache is not None:
                            await _reserve_next_refresh(recovery, book_id, cache)
                    await recovery.commit()
            except Exception as persistence_error:
                logger.warning(
                    "Échec non persisté pour le livre %s, erreur=%s",
                    book_id, type(persistence_error).__name__,
                )
        return None


def schedule_book_pricing(book_id: uuid.UUID, *, reason: str) -> None:
    """Cote un livre en arrière-plan, une seule fois par livre à la fois.

    Plusieurs annonces de la même BD peuvent tomber dans la même seconde ; sans
    ce garde-fou, chacune déclencherait sa propre salve chez les marchands.
    """
    with _pricing_lock:
        if book_id in _pricing_in_flight:
            logger.debug("Cotation déjà en vol pour le livre ID %s (%s).", book_id, reason)
            return
        _pricing_in_flight.add(book_id)
    parent = alert_latency.current()
    with alert_latency.timing_context(replace(parent, book_id=book_id) if parent else None):
        safe_create_task(
            _price_in_background(book_id, reason),
            name=f"market_price_{book_id}",
        )


@observe_background("book_pricing")
async def _price_in_background(book_id: uuid.UUID, reason: str) -> None:
    """Exécute la cotation puis libère le livre pour une nouvelle passe."""
    try:
        await price_book_now(book_id, reason=reason)
    finally:
        _pricing_in_flight.discard(book_id)
