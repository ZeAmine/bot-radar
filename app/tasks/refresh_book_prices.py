"""Tâche planifiée de rafraîchissement des prix de marché des BD et comics.

Le job interrogé les agrégateurs d'occasion (Chasse-aux-livres en tête, puis
Momox, Recyclivre, Gibert et eBay si configuré) et consolide pour chaque livre :

- le prix livré le plus bas réellement disponible chez un marchand francophone ;
- le prix le plus bas constaté sur 1 an, certifié par l'agrégateur.

Une passe se déroule par **vagues concurrentes bornées** : la pression sur les
partenaires reste identique (une pause anti-rate-limit par vague), mais un livre
lent ne bloque plus toute la file. Chaque livre dispose en plus de sa propre
session : un échec réseau ne contamine jamais le cache des autres.
"""

import asyncio
import logging
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.models.book import Book
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.db.session import AsyncSessionLocal
from app.providers.base import MarketOffer, extract_best_historical_low, normalize_search_isbn
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

# Durée maximale d'interrogation d'un livre, tous fournisseurs compris.
BOOK_FETCH_TIMEOUT_SECONDS = 20.0
# Nombre de livres interrogés simultanément.
BOOK_FETCH_CONCURRENCY = 4
# Durée maximale d'une passe : au-delà, les livres restants attendront la suivante.
MAX_PASS_DURATION_SECONDS = 900.0
DEFAULT_BATCH_SIZE = 8

# Durée de validité du cache selon la qualité de la donnée collectée.
CACHE_HOURS_WITH_PRICE = 24
CACHE_HOURS_WITHOUT_PRICE = 6
# Rafraîchissement minimal du cache conservé pour résilience après un silence réseau.
CACHE_HOURS_RESILIENT_REFRESH = 6

# Pause anti-rate-limit entre deux vagues, en mode incrémental puis complet.
PAUSE_RANGES: dict[bool, tuple[float, float]] = {
    True: (1.2, 2.2),
    False: (2.5, 3.8),
}

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
        )


def _iter_chunks[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    """Découpe une séquence en vagues de taille ``size``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def _select_books(
    session: AsyncSession | None,
    incremental: bool,
    batch_size: int,
) -> list[Book]:
    """Liste les livres à rafraîchir, en session dédiée en production."""

    async def _read(read_session: AsyncSession) -> list[Book]:
        book_repo = BookRepository(read_session)
        if incremental:
            return await book_repo.get_books_needing_price_refresh(limit=batch_size)
        return await book_repo.get_books_for_active_bd_alerts()

    if session is not None:
        return await _read(session)
    async with AsyncSessionLocal() as read_session:
        return await _read(read_session)


async def _persist_market_result(
    session: AsyncSession,
    book_id: UUID,
    result: BookPricingResult,
) -> str:
    """Enregistre snapshots et cache consolidé pour un livre. Retourne la statistique."""
    repo = MarketPriceRepository(session)
    for offer in result.offers:
        await repo.save_snapshot(book_id, offer)

    if not result.has_data:
        # Règle de résilience : un silence réseau ne doit pas priver l'utilisateur
        # d'un prix déjà connu et encore frais.
        existing = await repo.get_cache_by_book_id(book_id)
        if (
            existing is not None
            and existing.comparison_status == ComparisonStatus.AVAILABLE
            and existing.lowest_delivered_price_cents is not None
        ):
            logger.info(
                "Maintien résilient du prix en cache pour le livre ID %s (%s cents).",
                book_id,
                existing.lowest_delivered_price_cents,
            )
            now = datetime.now(UTC)
            existing.checked_at = now
            existing.expires_at = now + timedelta(hours=CACHE_HOURS_RESILIENT_REFRESH)
            return "available"

    await repo.update_or_create_cache(
        book_id=book_id,
        lowest_delivered_price_cents=result.lowest_delivered_price_cents,
        best_provider_name=result.best_provider_name,
        best_source_url=result.best_source_url,
        confidence=result.confidence,
        comparison_status=result.comparison_status,
        cache_duration_hours=result.cache_duration_hours,
        historical_low_1y_cents=result.historical_low_1y_cents,
    )
    return "available" if result.comparison_status == ComparisonStatus.AVAILABLE else "unavailable"


async def _price_one_book(
    book: Book,
    registry: ProviderRegistry,
    session: AsyncSession | None,
    semaphore: asyncio.Semaphore,
) -> str:
    """Interroge les fournisseurs pour un livre et consolide sa cote."""
    async with semaphore:
        offers = await asyncio.wait_for(
            registry.fetch_all_offers(
                isbn13=normalize_search_isbn(book.isbn13),
                title=book.title,
                author=book.author,
                edition=book.edition,
            ),
            timeout=BOOK_FETCH_TIMEOUT_SECONDS,
        )

    result = BookPricingResult.from_offers(
        offers=offers,
        best_offer=registry.select_best_offer(offers),
        historical_low_cents=extract_best_historical_low(offers),
    )

    if session is not None:
        return await _persist_market_result(session, book.id, result)

    async with AsyncSessionLocal() as book_session:
        outcome = await _persist_market_result(book_session, book.id, result)
        await book_session.commit()
        return outcome


async def _run_wave(
    wave: Sequence[Book],
    registry: ProviderRegistry,
    session: AsyncSession | None,
    semaphore: asyncio.Semaphore,
    stats: dict[str, int],
) -> None:
    """Traite une vague de livres et ventile les résultats dans les statistiques."""
    outcomes = await asyncio.gather(
        *(_price_one_book(book, registry, session, semaphore) for book in wave),
        return_exceptions=True,
    )
    for book, outcome in zip(wave, outcomes, strict=True):
        stats["processed"] += 1
        if isinstance(outcome, BaseException):
            logger.error(
                "Erreur lors du rafraîchissement pour le livre ID %s (%s): %s",
                book.id,
                book.title,
                outcome,
            )
            stats["errors"] += 1
        else:
            stats[outcome] += 1


async def refresh_book_prices_task(
    registry: ProviderRegistry | None = None,
    session: AsyncSession | None = None,
    incremental: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    concurrency: int = BOOK_FETCH_CONCURRENCY,
    max_duration_seconds: float = MAX_PASS_DURATION_SECONDS,
) -> dict[str, int]:
    """Rafraîchit les prix de marché pour les livres associés à des alertes BD actives.

    Args:
        registry: Registre des fournisseurs de marché (par défaut: tous les providers).
        session: Session SQLAlchemy optionnelle (utilisée principalement par les tests).
        incremental: Si True, traite uniquement un micro-lot prioritaire (cache expiré ou absent)
                     afin de lisser la charge 24h/24 sans aucun pic de performance.
        batch_size: Taille du lot en mode incrémental (défaut: 8 livres).
        concurrency: Livres interrogés simultanément (défaut: 4).
        max_duration_seconds: Plafond de durée de la passe ; les livres non atteints
                              sont reportés au prochain passage du scheduler.
    """
    if registry is None:
        registry = ProviderRegistry()

    stats = {"processed": 0, "available": 0, "unavailable": 0, "errors": 0}
    books = await _select_books(session, incremental, batch_size)

    if not books:
        logger.debug("Aucun livre BD à rafraîchir pour cette passe.")
        return stats

    mode_label = (
        f"incrémental ({len(books)} livres)"
        if incremental
        else f"complet ({len(books)} livres)"
    )
    logger.info("Début du rafraîchissement des prix BD en mode %s.", mode_label)

    semaphore = asyncio.Semaphore(max(1, concurrency))
    wave_size = max(1, concurrency)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_duration_seconds

    for wave in _iter_chunks(books, wave_size):
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning(
                "Plafond de durée atteint : %d/%d livres refreshés, reste reporté.",
                stats["processed"],
                len(books),
            )
            break
        await _run_wave(wave, registry, session, semaphore, stats)

        has_more = stats["processed"] < len(books)
        if has_more:
            # Pause anti-rate-limit une fois par vague : la pression sur les
            # partenaires reste équivalente à un traitement séquentiel.
            await asyncio.sleep(random.uniform(*PAUSE_RANGES[bool(incremental)]))

    logger.info(
        "Rafraîchissement BD terminé (%s) : %d traités, %d avec prix, "
        "%d indisponibles, %d erreurs.",
        mode_label,
        stats["processed"],
        stats["available"],
        stats["unavailable"],
        stats["errors"],
    )
    return stats
