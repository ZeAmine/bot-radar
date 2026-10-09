"""Cotation d'un livre : un seul chemin des fournisseurs vers le cache local.

Quatre appelants ont besoin d'une cote — la création d'une alerte, l'édition d'un
ISBN, le bouton « 📊 Prix du marché » et le chemin d'alerte quand elle part sans
prix. Chacun avait sa copie, et elles ne faisaient pas la même chose : la
création n'interrogeait qu'un agrégateur par ISBN, sans repli par titre ni
marchands directs, et lisait l'historique 1 an sur la seule meilleure offre, ce
qui le faisait disparaître dès qu'un autre marchand proposait moins cher. Un
livre laissé sans cote n'est jamais rattrapé ensuite : le job glissant ne traite
que les livres déjà liés à une alerte.

Deux formes d'appel, une seule implémentation :

- `price_book_now` : le résultat est attendu, pour un écran qui doit l'afficher ;
- `schedule_book_pricing` : en arrière-plan, pour tout ce qui ne doit jamais
  retarder l'envoi d'une alerte (invariant n°1).
"""

import asyncio
import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ComparisonStatus, MatchConfidence
from app.core.tasks import safe_create_task
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.db.session import AsyncSessionLocal
from app.domain.rules.market_offer import is_comparable_secondhand_offer
from app.providers.base import (
    MarketOffer,
    MarketOffers,
    extract_best_historical_low,
    normalize_search_isbn,
)
from app.providers.registry import ProviderRegistry
from app.services.market_price_service import series_reference_total
from app.utils.price import format_cents

logger = logging.getLogger(__name__)

# Durée maximale d'interrogation d'un livre, tous fournisseurs compris.
BOOK_FETCH_TIMEOUT_SECONDS = 20.0
# Rafraîchissement minimal du cache conservé pour résilience après un silence réseau.
CACHE_HOURS_RESILIENT_REFRESH = 6
# Durée de validité du cache selon la qualité de la donnée collectée.
CACHE_HOURS_WITH_PRICE = 24
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

    @property
    def has_data(self) -> bool:
        """Vrai si cette passe a réellement trouvé de quoi coter le livre."""
        return bool(
            self.lowest_delivered_price_cents is not None
            or self.historical_low_1y_cents is not None
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
) -> list[MarketOffer]:
    """Interroge tous les fournisseurs pour un livre, ISBN valide et titre ensemble.

    Le titre accompagne toujours l'ISBN : un agrégateur qui ne trouve rien par
    code sait retrouver l'ouvrage par son titre, et c'était précisément ce repli
    qui manquait à la création d'une alerte — d'où une cote vide en base, donc
    une alerte partie sans prix alors que le bouton, lui, trouvait la cote.
    """
    active_registry = registry or ProviderRegistry()
    try:
        return await asyncio.wait_for(
            active_registry.fetch_all_offers(
                isbn13=normalize_search_isbn(book.isbn13),
                title=book.title,
                author=book.author,
                edition=book.edition,
            ),
            timeout=BOOK_FETCH_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        names = tuple(provider.name for provider in active_registry.providers)
        logger.warning("Délai global de collecte dépassé pour %s", book.id)
        return MarketOffers(failed_providers=names or ("collecte marché",))


async def _refresh_series_totals(session: AsyncSession, book_id: uuid.UUID) -> None:
    """Réécrit le total des séries qui portent ce livre — après chaque écriture de cote.

    C'est la **seule** adresse où le total est écrit (invariant : une seule
    écriture, un seul chemin). Une alerte sans ISBN mais à un seul livre relié
    n'est pas une série : son total reste ``NULL``, l'écran n'a rien à sommer.
    """
    # En production autoflush=False : populate_existing écraserait autrement
    # les métadonnées de collecte encore en mémoire lors de la relecture.
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

    if not result.has_data:
        # Règle de résilience : un silence réseau ne doit pas priver l'utilisateur
        # d'un prix déjà connu et encore frais.
        existing = await repo.get_cache_by_book_id(book_id)
        if existing is not None and (
            result.failed_providers
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
            # Un silence réseau n'empêche pas de réparer une cote surestimée :
            # nos relevés sont déjà en base.
            if existing.historical_low_1y_cents is not None:
                existing.historical_low_1y_cents = await repo.clamp_historical_low_by_observations(
                    book_id, existing.historical_low_1y_cents
                )
            now = datetime.now(UTC)
            _stamp_collection(existing, result)
            existing.expires_at = now + timedelta(hours=CACHE_HOURS_RESILIENT_REFRESH)
            await _refresh_series_totals(session, book_id)
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
    )
    _stamp_collection(cache, result)
    await _refresh_series_totals(session, book_id)
    return cache


def _stamp_collection(cache: BookMarketCache, result: BookPricingResult) -> None:
    """Conserve le résultat de tentative séparément de la date du dernier prix."""
    cache.last_attempt_at = datetime.now(UTC)
    cache.failed_provider_names = list(result.failed_providers)
    retained_offers = cache.current_offers
    cache.current_offers = [
        asdict(offer)
        for offer in result.offers
        if is_comparable_secondhand_offer(offer)
        and offer.is_available
        and (offer.shipping_price_cents is not None or offer.price_includes_shipping)
    ]
    if result.failed_providers and not result.has_data:
        cache.current_offers = retained_offers
    cache.collection_status = (
        "partial"
        if result.failed_providers and result.has_data
        else "failed"
        if result.failed_providers
        else "complete"
        if result.has_data
        else "empty"
    )


async def price_book(
    session: AsyncSession,
    book: Book,
    registry: ProviderRegistry | None = None,
) -> BookMarketCache:
    """Cote un livre et écrit sa cote dans la session courante (sans commit)."""
    offers = await fetch_offers_for_book(book, registry=registry)
    result = BookPricingResult.from_offers(
        offers=offers,
        best_offer=ProviderRegistry.select_best_offer(offers),
        historical_low_cents=extract_best_historical_low(offers),
    )
    cache = await persist_market_result(session, book.id, result)
    logger.info(
        "Cote du livre ID %s (%s) : marché %s, plus bas 1 an %s (%s).",
        book.id,
        book.title,
        format_cents(cache.lowest_delivered_price_cents)
        if cache.lowest_delivered_price_cents
        else "inconnu",
        format_cents(cache.historical_low_1y_cents) if cache.historical_low_1y_cents else "inconnu",
        pricing_stat(cache),
    )
    return cache


_pricing_tasks: dict[uuid.UUID, asyncio.Task[BookMarketCache | None]] = {}


async def price_book_now(
    book_id: uuid.UUID,
    *,
    reason: str,
    registry: ProviderRegistry | None = None,
) -> BookMarketCache | None:
    """Partage une collecte déjà en vol entre le scheduler et les panneaux."""
    task = _pricing_tasks.get(book_id)
    if task is None:
        task = safe_create_task(
            _price_book_once(book_id, reason=reason, registry=registry),
            name=f"price_book_{book_id}",
        )
        _pricing_tasks[book_id] = task
        task.add_done_callback(lambda _done: _pricing_tasks.pop(book_id, None))
    return await asyncio.shield(task)


async def _price_book_once(
    book_id: uuid.UUID,
    *,
    reason: str,
    registry: ProviderRegistry | None = None,
) -> BookMarketCache | None:
    """Cote un livre dans sa propre session. N'élève jamais.

    Un marchand muet n'a pas le droit de faire échouer l'écran qui a demandé la
    cote : l'appelant reçoit ``None`` et affiche ce qu'il sait déjà.
    """
    try:
        async with AsyncSessionLocal() as session:
            book = await BookRepository(session).get_by_id(book_id)
            if book is None:
                logger.warning("Cotation ignorée : livre ID %s introuvable (%s).", book_id, reason)
                return None
            cache = await price_book(session, book, registry=registry)
            await session.commit()
            return cache
    except Exception as exc:
        logger.warning("Cotation du livre ID %s impossible (%s) : %s", book_id, reason, exc)
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
    safe_create_task(
        _price_in_background(book_id, reason),
        name=f"market_price_{book_id}",
    )


async def _price_in_background(book_id: uuid.UUID, reason: str) -> None:
    """Exécute la cotation puis libère le livre pour une nouvelle passe."""
    try:
        await price_book_now(book_id, reason=reason)
    finally:
        _pricing_in_flight.discard(book_id)
