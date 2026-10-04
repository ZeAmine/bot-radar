"""Tâche planifiée de rafraîchissement des prix de marché des BD et comics.

Le job passe par le service de cotation commun : interroger tous les
fournisseurs, consolider le prix livré le plus bas et le prix le plus bas
constaté sur 1 an, puis écrire le cache local. La création d'une alerte et le
bouton « 📊 Prix du marché » passent par exactement la même consolidation —
cette tâche n'en est qu'un client parmi d'autres.

Une passe se déroule par **vagues concurrentes bornées** : la pression sur les
partenaires reste identique (une pause anti-rate-limit par vague), mais un livre
lent ne bloque plus toute la file. Chaque livre dispose en plus de sa propre
session : un échec réseau ne contamine jamais le cache des autres.
"""

import asyncio
import logging
import random
from collections.abc import Iterable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.book import Book
from app.db.repositories.book_repo import BookRepository
from app.db.session import AsyncSessionLocal
from app.providers.base import MarketOffer, extract_best_historical_low
from app.providers.registry import ProviderRegistry
from app.services.book_market_pricing import (
    BookPricingResult,
    fetch_offers_for_book,
    persist_market_result,
    pricing_stat,
)
from app.services.market_health import notify_stale_certified_lows

logger = logging.getLogger(__name__)

# Nombre de livres interrogés simultanément.
BOOK_FETCH_CONCURRENCY = 4
# Durée maximale d'une passe : au-delà, les livres restants attendront la suivante.
MAX_PASS_DURATION_SECONDS = 900.0
DEFAULT_BATCH_SIZE = 8

# Pause anti-rate-limit entre deux vagues, en mode incrémental puis complet.
PAUSE_RANGES: dict[bool, tuple[float, float]] = {
    True: (1.2, 2.2),
    False: (2.5, 3.8),
}


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


async def _price_one_book(
    book: Book,
    registry: ProviderRegistry,
    session: AsyncSession | None,
    semaphore: asyncio.Semaphore,
) -> str:
    """Interroge les fournisseurs pour un livre et consolide sa cote."""
    async with semaphore:
        offers: list[MarketOffer] = await fetch_offers_for_book(book, registry=registry)

    result = BookPricingResult.from_offers(
        offers=offers,
        best_offer=registry.select_best_offer(offers),
        historical_low_cents=extract_best_historical_low(offers),
    )

    if session is not None:
        return pricing_stat(await persist_market_result(session, book.id, result))

    async with AsyncSessionLocal() as book_session:
        outcome = pricing_stat(await persist_market_result(book_session, book.id, result))
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


async def _report_stale_certs(session: AsyncSession | None) -> None:
    """Signale les cotes que plus aucun relevé ne nourrit.

    Le contrôle tourne dans sa propre session quand la passe en a utilisé une :
    celle-ci peut être en fin de vie après des centaines de livres, et une alerte
    de maintenance ne mérite pas de faire échouer le job qui vient de réussir.
    """
    if session is not None:
        await notify_stale_certified_lows(session)
        return
    async with AsyncSessionLocal() as health_session:
        await notify_stale_certified_lows(health_session)


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

    # Une passe peut réussir sur tous les livres sans avoir nourri une seule cote :
    # c'est ce qui s'est produit pendant trois semaines. Le signal part ici, une
    # fois par jour au plus, et jamais au prix du rafraîchissement.
    await _report_stale_certs(session)

    return stats
