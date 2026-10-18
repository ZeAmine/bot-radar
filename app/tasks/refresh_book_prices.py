"""Rafraîchissement progressif des composantes de prix éligibles."""

import asyncio
import logging
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import pilot_mode
from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.book_repo import BookRepository
from app.db.session import AsyncSessionLocal
from app.domain.enums import AlertStatus
from app.providers.base import CollectionAttempt, MarketOffer, extract_best_historical_low
from app.providers.registry import ProviderRegistry
from app.services.book_market_pricing import (
    BookPricingResult,
    fetch_offers_for_book,
    persist_market_result,
    price_book_now,
    pricing_stat,
)
from app.services.market_health import (
    RefreshPassResult,
    notify_refresh_result,
    read_refresh_health,
)
from app.services.market_refresh_policy import reservation_lock

logger = logging.getLogger(__name__)

# Nombre de livres interrogés simultanément.
BOOK_FETCH_CONCURRENCY = 2
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

    _ = incremental

    async def _read(read_session: AsyncSession) -> list[Book]:
        book_repo = BookRepository(read_session)
        async with reservation_lock():
            await book_repo.plan_price_refreshes(capacity=batch_size)
            await read_session.commit()
        return await book_repo.get_books_needing_price_refresh(limit=batch_size)

    if session is not None:
        return await _read(session)
    async with AsyncSessionLocal() as read_session:
        return await _read(read_session)


def _pass_stat(cache: BookMarketCache | None) -> str:
    """Ventile la passe : un fournisseur muet dégrade la collecte, il ne la plante pas.

    ``pricing_stat`` dit « errors » dès que la collecte est ``failed``. Pour le
    seuil d'alerte, ce mot est réservé à une exception ou à une cotation qui
    n'a rien pu écrire. Le livre, lui, a un résultat : la source nommée a échoué.
    """
    stat = pricing_stat(cache)
    if stat == "errors":
        return "degraded"
    return stat


@dataclass(frozen=True)
class BookRefreshResult:
    verdict: str
    refreshed: bool
    attempts: tuple[CollectionAttempt, ...] = ()
    empty: bool = False
    reused: bool = False


def _book_result(
    cache: BookMarketCache | None, result: BookPricingResult | None,
) -> BookRefreshResult:
    if result is None:
        return BookRefreshResult(_pass_stat(cache), False, reused=cache is not None)
    current = result.lowest_delivered_price_cents is not None
    verdict = "degraded" if result.failed_providers else "available" if current else "unavailable"
    return BookRefreshResult(verdict, current, result.attempts,
                             not current and result.current_collection_succeeded)


async def _price_one_book(
    book: Book,
    registry: ProviderRegistry,
    session: AsyncSession | None,
    semaphore: asyncio.Semaphore,
) -> BookRefreshResult:
    """Interroge les fournisseurs pour un livre et consolide sa cote.

    Rend le verdict de la passe **et** le fait qu'une cote utilisable ait été écrite.
    Les deux sont calculés ici, au seul endroit qui voit le résultat de la collecte :
    les déduire plus tard, depuis les statistiques agrégées, reviendrait à deviner.
    """
    if session is None:
        async with semaphore:
            results: list[BookPricingResult] = []
            cache = await price_book_now(book.id, reason="scheduled", registry=registry,
                                         result_sink=results)
        if cache is None:
            return BookRefreshResult("errors", False)
        registry.failed_provider_names.update(cache.failed_provider_names or [])
        return _book_result(cache, results[-1] if results else None)
    async with semaphore:
        offers: list[MarketOffer] = await fetch_offers_for_book(book, registry=registry)

    result = BookPricingResult.from_offers(
        offers=offers,
        best_offer=registry.select_best_offer(offers),
        historical_low_cents=extract_best_historical_low(offers),
    )

    cache = await persist_market_result(session, book.id, result)
    registry.failed_provider_names.update(cache.failed_provider_names or [])
    return _book_result(cache, result)


async def _run_wave(
    wave: Sequence[Book],
    registry: ProviderRegistry,
    session: AsyncSession | None,
    semaphore: asyncio.Semaphore,
    stats: dict[str, int],
    attempts: list[CollectionAttempt] | None = None,
    occurrence: str | None = None,
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
            stats[outcome.verdict] += 1
            if outcome.reused:
                stats["reused"] = stats.get("reused", 0) + 1
            if outcome.empty:
                stats["empty"] = stats.get("empty", 0) + 1
            if attempts is not None:
                attempts.extend(outcome.attempts)
            for attempt in outcome.attempts:
                logger.info("Tentative de passe", extra={
                    "book_id": str(book.id), "occurrence": occurrence,
                    "provider": attempt.provider, "operation": attempt.operation,
                    "outcome": attempt.status, "reason": attempt.reason,
                    "http_status": attempt.http_status,
                    "duration_seconds": attempt.duration_seconds,
                })
            if outcome.refreshed:
                stats["refreshed"] += 1


def _occurrence_token(
    job_id: str, *, scheduled_for: datetime | None = None, now: datetime | None = None,
) -> str:
    """Identifie l'exécution **prévue** d'un job, pour dédoublonner son bilan.

    L'identité est celle de l'exécution *planifiée*, pas de l'instant où le code
    tourne : c'est ce qui permet à une reprise de retrouver l'occurrence qu'elle
    reprend. `scheduled_for` est l'instant que le planificateur visait, transmis par
    l'enveloppe du job (`app/tasks/scheduler.py`) ; sans lui, on retombe sur l'heure
    courante, ce qui suffit hors planificateur mais ne distingue plus une exécution
    retardée de l'exécution suivante.

    Le jeton est figé au **début** de la passe et voyage avec elle : une collecte qui
    traverse une frontière de tranche garde donc son identité jusqu'à sa notification.

    Deux exécutions de la même occurrence — une reprise, un doublon d'ordonnancement —
    produisent un seul bilan ; deux occurrences distinctes produisent chacune le leur.
    Cette garantie est **bornée par le TTL** de la clé (voir
    `INCOMPLETE_NOTIFY_TTL_SECONDS`) : au-delà, la clé a expiré et une nouvelle
    tentative repartirait pour un second bilan. La borne est choisie pour couvrir la
    fenêtre de réexécution réellement prise en charge, pas pour être éternelle.
    """
    moment = scheduled_for or now or datetime.now(UTC)
    slot = moment.replace(minute=moment.minute - moment.minute % 30, second=0, microsecond=0)
    return f"{job_id}@{slot.isoformat()}"


async def resolve_scheduled_catalogues(*, incremental: bool, deadline: float) -> None:
    """Réutilise la résolution existante sans activer une alerte en pause."""
    from app.use_cases.view_market_details import ViewMarketDetailsUseCase

    async with AsyncSessionLocal() as reader:
        ids = await AlertRepository(reader).scheduled_catalogue_ids(incremental=incremental)
    for alert_id in ids:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            async with AsyncSessionLocal() as session:
                alert = await session.get(Alert, alert_id)
                if alert is None or alert.status == AlertStatus.DELETED or alert.preparation:
                    continue
                alert.catalogue_refresh_attempted_at = datetime.now(UTC)
                await session.commit()
                async with asyncio.timeout(min(45.0, remaining)):
                    await ViewMarketDetailsUseCase(session).resolve_alert_books(
                        alert_id, force_catalogue=True,
                    )
                if not await BookRepository(session).get_all_books_for_alert(alert_id):
                    logger.warning("Cotation sans ouvrage identifié : alerte %s", alert_id)
        except Exception as exc:
            logger.warning("Résolution planifiée impossible : alerte %s (%s)",
                           alert_id, type(exc).__name__)


_pass_loop: asyncio.AbstractEventLoop | None = None
_pass_lock: asyncio.Lock | None = None


async def refresh_book_prices_task(
    registry: ProviderRegistry | None = None,
    session: AsyncSession | None = None,
    incremental: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    concurrency: int = BOOK_FETCH_CONCURRENCY,
    max_duration_seconds: float = MAX_PASS_DURATION_SECONDS,
    job_id: str | None = None,
    scheduled_for: datetime | None = None,
) -> dict[str, int]:
    if pilot_mode.active():
        return dict.fromkeys(("processed", "refreshed", "empty", "reused", "available",
                              "unavailable", "degraded", "errors"), 0)
    global _pass_loop, _pass_lock
    loop = asyncio.get_running_loop()
    if _pass_lock is None or _pass_loop is not loop:
        _pass_loop, _pass_lock = loop, asyncio.Lock()
    if _pass_lock.locked():
        return dict.fromkeys(("processed", "refreshed", "empty", "reused", "available",
                              "unavailable", "degraded", "errors"), 0)
    async with _pass_lock:
        return await _refresh_book_prices_pass(
            registry=registry, session=session, incremental=incremental,
            batch_size=batch_size, concurrency=concurrency,
            max_duration_seconds=max_duration_seconds, job_id=job_id, scheduled_for=scheduled_for,
        )


async def _refresh_book_prices_pass(
    registry: ProviderRegistry | None = None,
    session: AsyncSession | None = None,
    incremental: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    concurrency: int = BOOK_FETCH_CONCURRENCY,
    max_duration_seconds: float = MAX_PASS_DURATION_SECONDS,
    job_id: str | None = None,
    scheduled_for: datetime | None = None,
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
        job_id: Identifiant du job planifié qui a déclenché la passe. Il sert à
                identifier l'**occurrence** pour le bilan : deux tentatives de la
                même exécution prévue partagent ce nom et la même tranche horaire,
                deux exécutions distinctes non.
        scheduled_for: Instant que le planificateur **visait** pour cette exécution.
                       C'est lui qui date l'occurrence, jamais l'heure à laquelle le
                       code tourne : une passe de 19:00 lancée à 19:31 reste
                       l'occurrence de 19:00.
    """
    if registry is None:
        if session is None:
            from app.providers.chasse_session import chasse_session
            await chasse_session.maintain()
        registry = ProviderRegistry()

    occurrence = _occurrence_token(job_id, scheduled_for=scheduled_for) if job_id else None

    stats = {
        "processed": 0,
        # Distinct de « processed » : un livre traité dont les fournisseurs sont
        # muets n'a rien écrit. C'est lui qui dit si la passe a servi à quelque chose.
        "refreshed": 0,
        "empty": 0,
        "reused": 0,
        "available": 0,
        "unavailable": 0,
        "degraded": 0,
        "errors": 0,
    }
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_duration_seconds
    # Passe coupée par le plafond de durée : fonctionnement prévu d'un lot qui ne
    # tient pas dans sa fenêtre, transmis comme information et non comme alerte.
    interrupted = False
    report = RefreshPassResult(0, stats, incremental, occurrence, selection_known=False)
    try:
        if session is None:
            await resolve_scheduled_catalogues(incremental=incremental, deadline=deadline)
        books = await _select_books(session, incremental, batch_size)


        mode_label = (
            f"incrémental ({len(books)} livres)" if incremental
            else f"complet ({len(books)} livres)"
        )
        logger.info("Début du rafraîchissement des prix BD en mode %s.", mode_label)

        report.total = len(books)
        report.selection_known = True
        semaphore = asyncio.Semaphore(max(1, concurrency))
        wave_size = max(1, concurrency)
        for wave in _iter_chunks(books, wave_size):
            remaining = deadline - loop.time()
            if remaining <= 0:
                interrupted = True
                logger.info(
                    "Plafond de durée atteint : %d/%d livres traités, le reste attend "
                    "la passe suivante.",
                    stats["processed"],
                    len(books),
                )
                break
            await _run_wave(wave, registry, session, semaphore, stats, report.attempts, occurrence)

            has_more = stats["processed"] < len(books)
            if has_more:
                # Pause anti-rate-limit une fois par vague : la pression sur les
                # partenaires reste équivalente à un traitement séquentiel.
                await asyncio.sleep(random.uniform(*PAUSE_RANGES[bool(incremental)]))

        logger.info(
            "Rafraîchissement BD terminé (%s) : %d traités, %d mis à jour, "
            "%d indisponibles, %d dégradés, %d erreurs.",
            mode_label,
            stats["processed"],
            stats["refreshed"],
            stats["unavailable"],
            stats["degraded"],
            stats["errors"],
        )
    except (Exception, asyncio.CancelledError):
        report.technical_interruption = True
        logger.exception("Passe de rafraîchissement interrompue", extra={"occurrence": occurrence})
        raise
    finally:
        report.interrupted = interrupted
        try:
            if session is not None:
                await read_refresh_health(report, session)
            else:
                async with AsyncSessionLocal() as health_session:
                    await read_refresh_health(report, health_session)
        except Exception:
            logger.warning("Diagnostic de fin de passe indisponible", exc_info=True)
        await notify_refresh_result(report)

    await _select_books(session, incremental, batch_size)
    return stats
