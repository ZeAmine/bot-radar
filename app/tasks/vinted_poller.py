"""Tâche d'arrière-plan de surveillance et d'ingestion automatique du flux Vinted."""

import asyncio
import contextlib
import json
import logging
import random
import time
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from app.core import alert_latency as latency
from app.core import pilot_mode
from app.core.config import settings
from app.core.constants import AlertStatus, CategorySlug
from app.core.redis import get_redis_client
from app.core.tasks import safe_create_task
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.listing_repo import ListingRepository
from app.db.session import AsyncSessionLocal
from app.services.telegram_service import notify_admins
from app.services.vinted_parsing import CATALOG_PAGE_SIZE
from app.services.vinted_scraper import (
    DEFAULT_GLOBAL_BD_URL,
    VintedBlockError,
    VintedCollectionError,
    VintedRateLimitError,
    fetch_vinted_page,
    to_ingest_payload,
    vinted_request_stats,
    vinted_session_manager,
)
from app.utils.percentile import interpolated_percentile

logger = logging.getLogger("radar.vinted_poller")

VINTED_POLLER_STATUS_KEY = "radar:vinted_poller_status"

WINDOW_LOG_EVERY_CYCLES = 20

CYCLE_GAP_SAMPLES = 40

MAX_MEANINGFUL_CYCLE_GAP_S = 90.0

MIN_CYCLE_GAP_SECONDS = 6.0

COLLECTION_FAILURE_ALERT_THRESHOLD = 5
AVAILABILITY_CATEGORIES = ("scanned", "night", "blocked", "network_error", "unexpected_error")
AVAILABILITY_WINDOW_SECONDS = 24 * 3600.0

class CycleCadence:
    """Historique des écarts entre deux cycles de scrutation.

    La cadence décide de la position dans la file : une annonce publiée entre deux
    cycles n'est vue qu'au suivant. Elle était une constante codée en dur, invisible
    donc non vérifiable ; la rendre mesurée permet de voir une cadence dégradée au
    lieu de la lire comme normale. L'interpolation est celle de la latence
    (`interpolated_percentile`) : les deux s'affichent dans le même `/status`.
    """

    def __init__(self, samples: int = CYCLE_GAP_SAMPLES) -> None:
        self.gaps: deque[float] = deque(maxlen=samples)
        self._last_at: float | None = None

    def record(self, now_ts: float) -> None:
        """Enregistre l'écart depuis le cycle précédent.

        Le premier cycle n'ouvre pas d'échantillon : son écart porterait sur
        l'initialisation, pas sur la cadence. Les écarts plus longs que
        `MAX_MEANINGFUL_CYCLE_GAP_S` sont ignorés : ce sont des interruptions — reprise
        du mode nuit, fin d'une pause anti-blocage — et non des cycles de scrutation.
        """
        if self._last_at is not None:
            gap = now_ts - self._last_at
            if 0 < gap <= MAX_MEANINGFUL_CYCLE_GAP_S:
                self.gaps.append(gap)
        self._last_at = now_ts

    def percentile(self, pct: float) -> float | None:
        """Percentile des écarts, arrondi à 0,1 s. None si l'échantillon est vide."""
        value = interpolated_percentile(self.gaps, pct)
        return None if value is None else round(value, 1)


class AvailabilityLedger:
    """Secondes écoulées par catégorie de cycle, sur une fenêtre glissante bornée."""

    def __init__(self, window_seconds: float = AVAILABILITY_WINDOW_SECONDS) -> None:
        self._window_seconds = window_seconds
        self._events: deque[tuple[float, str, float]] = deque()
        self._seconds: dict[str, float] = dict.fromkeys(AVAILABILITY_CATEGORIES, 0.0)

    def add(self, category: str, seconds: float, now: float | None = None) -> None:
        if category not in self._seconds:
            raise ValueError(f"catégorie de disponibilité inconnue : {category}")
        duration = max(0.0, seconds)
        if duration == 0.0:
            return
        moment = time.monotonic() if now is None else now
        self._events.append((moment, category, duration))
        self._seconds[category] += duration
        self._trim(moment)

    def snapshot(self, now: float | None = None) -> dict[str, float]:
        moment = time.monotonic() if now is None else now
        self._trim(moment)
        snapshot = {"window_s": round(sum(self._seconds.values()), 1)}
        for category, seconds in self._seconds.items():
            snapshot[f"{category}_s"] = round(seconds, 1)
        return snapshot

    def _trim(self, now: float) -> None:
        while self._events and now - self._events[0][0] > self._window_seconds:
            _, category, duration = self._events.popleft()
            self._seconds[category] -= duration


def search_freshness_snapshot(
    first_seen: dict[tuple[str, str], float],
    last_success: dict[tuple[str, str], float],
    now: float,
) -> dict[str, float] | None:
    """Âge du plus ancien dernier scan réussi parmi les recherches personnalisées."""
    if not first_seen:
        return None
    worst_age = max(now - last_success.get(key, seen) for key, seen in first_seen.items())
    return {"count": len(first_seen), "worst_age_s": round(worst_age, 1)}


def _request_delta(before: dict[str, int]) -> dict[str, int]:
    """Requêtes Vinted émises depuis le relevé `before`, par type."""
    after = vinted_request_stats.totals()
    return {kind: total - before.get(kind, 0) for kind, total in after.items()}


def _log_cycle_journal(
    cycle: int,
    outcome: str,
    duration_seconds: float,
    target_seconds: float | None,
    items: int,
    requests: dict[str, int],
) -> None:
    """Une ligne par cycle : issue, durée, annonces et requêtes émises."""
    logger.info(
        "Cycle Vinted #%d terminé (%s) en %.2fs : %d annonces.",
        cycle,
        outcome,
        duration_seconds,
        items,
        extra={
            "event": "poller_cycle",
            "cycle": cycle,
            "outcome": outcome,
            "duration_s": round(duration_seconds, 2),
            "target_s": target_seconds,
            "items": items,
            "requests": requests,
        },
    )


class FifoSet(set[str]):
    """Ensemble d'identifiants avec purge FIFO déterministe par ordre d'insertion."""

    def __init__(
        self,
        iterable: Iterable[str] = (),
        max_size: int = 15000,
        trim_size: int = 8000,
    ) -> None:
        super().__init__()
        self.max_size = max_size
        self.trim_size = trim_size
        self._order: deque[str] = deque()
        for item in iterable:
            self.add(item)

    def add(self, item: str) -> None:
        if item not in self:
            super().add(item)
            self._order.append(item)
            if len(self) > self.max_size:
                self.trim()

    def discard(self, item: str) -> None:  # type: ignore[override]
        if item in self:
            super().discard(item)
            with contextlib.suppress(ValueError):
                self._order.remove(item)

    def remove(self, item: str) -> None:
        super().remove(item)
        with contextlib.suppress(ValueError):
            self._order.remove(item)

    def trim(self) -> None:
        """Purge les identifiants les plus anciens jusqu'à trim_size."""
        while len(self._order) > self.trim_size:
            oldest = self._order.popleft()
            super().discard(oldest)


async def save_poller_status(data: dict[str, Any]) -> None:
    """Enregistre l'état actuel du poller dans Redis (TTL 24h)."""
    try:
        client = get_redis_client()
        await client.set(VINTED_POLLER_STATUS_KEY, json.dumps(data), ex=86400)
    except Exception as exc:
        logger.debug("Impossible d'enregistrer l'état du poller dans Redis: %s", exc)


async def get_poller_status() -> dict[str, Any] | None:
    """Récupère l'état actuel du poller depuis Redis."""
    try:
        client = get_redis_client()
        raw = await client.get(VINTED_POLLER_STATUS_KEY)
        if raw:
            data: dict[str, Any] = json.loads(raw)
            return data
    except Exception as exc:
        logger.debug("Impossible de lire l'état du poller depuis Redis: %s", exc)
    return None


def is_night_time(now: datetime | None = None) -> bool:
    """Vérifie si l'heure courante correspond aux heures creuses / mode nuit."""
    if not settings.NIGHT_MODE_ENABLED:
        return False
    try:
        tz = ZoneInfo(settings.NIGHT_MODE_TIMEZONE)
    except Exception:
        tz = ZoneInfo("Europe/Paris")

    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    start = settings.NIGHT_MODE_START_HOUR
    end = settings.NIGHT_MODE_END_HOUR
    hour = now.hour

    if start <= end:
        return start <= hour < end
    else:
        return hour >= start or hour < end


def get_night_mode_remaining_seconds(now: datetime | None = None) -> float:
    """Calcule le nombre de secondes restantes avant la fin du mode nuit."""
    try:
        tz = ZoneInfo(settings.NIGHT_MODE_TIMEZONE)
    except Exception:
        tz = ZoneInfo("Europe/Paris")

    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    end_hour = settings.NIGHT_MODE_END_HOUR
    target = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)

    return max(0.0, (target - now).total_seconds())


def is_peak_hours(now: datetime | None = None) -> bool:
    """Vérifie si l'heure courante correspond aux heures de forte activité sur Vinted."""
    try:
        tz = ZoneInfo(settings.NIGHT_MODE_TIMEZONE)
    except Exception:
        tz = ZoneInfo("Europe/Paris")

    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    hour = now.hour
    minute = now.minute
    time_float = hour + minute / 60.0

    lunch_peak = 11.5 <= time_float <= 14.0
    evening_peak = 18.0 <= time_float <= 22.5
    return lunch_peak or evening_peak


def get_adaptive_poll_interval() -> float:
    """Calcule l'intervalle cible de scrutation selon le contexte réseau et horaire.

    `VINTED_POLL_INTERVAL_SECONDS` est la cadence standard visée **et** un plafond des
    modes accélérés : la pointe (8 s / 12 s) et le proxy résidentiel (8 s / 10 s) ne
    peuvent que la raccourcir. La branche standard la sert telle quelle — un plancher
    la rendrait insensible à une valeur plus basse, ce qui était le cas avec l'ancien
    `max(base, 15.0)`.
    """
    base = float(settings.VINTED_POLL_INTERVAL_SECONDS)
    has_residential_proxy = bool(settings.get_vinted_proxy())

    if has_residential_proxy:
        return min(base, 8.0 if is_peak_hours() else 10.0)

    if is_peak_hours():
        return min(base, 12.0)
    return base


def remaining_cycle_sleep(sleep_time: float, work_seconds: float) -> float:
    """Sommeil restant une fois le travail du cycle déduit, sans l'y ajouter.

    La pause est une respiration entre deux scrutations, pas un délai qui s'ajoute à
    l'ingestion et à l'envoi Telegram. Le cycle vaut donc au moins la pause cible, et
    le travail ne la rallonge que s'il la dépasse — sans jamais descendre sous le
    plancher, qui garde une respiration minimale pendant une rafale d'annonces.
    """
    return max(MIN_CYCLE_GAP_SECONDS, sleep_time - max(0.0, work_seconds))


async def load_initial_seen_ids() -> FifoSet:
    """Recharge les vues récentes sans masquer une notification encore à envoyer."""
    try:
        async with AsyncSessionLocal() as session:
            raw_ids = await ListingRepository(session).get_initial_seen_external_ids("vinted")
            return FifoSet(reversed(raw_ids))
    except Exception as exc:
        logger.warning("Impossible de charger les anciens IDs d'annonces : %s", exc)
        return FifoSet()


def dedupe_custom_searches(searches: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Retire les couples (URL, catégorie) strictement identiques.

    Deux alertes de même catégorie partageant la même URL occuperaient sinon deux tours
    du round-robin : le même flux interrogé deux fois, au détriment des autres recherches.
    La catégorie fait partie de la clé — fusionner deux catégories sur une même URL
    changerait le matching, qui se fait par catégorie.
    """
    return list(dict.fromkeys(searches))


async def load_active_custom_searches() -> list[tuple[str, str]]:
    """Charge les URLs de recherche actives pour les alertes hors-livres (tech, vêtements).

    Retourne une liste de tuples (vinted_search_url, category_slug).
    """
    try:
        async with AsyncSessionLocal() as session:
            stmt = (
                select(Alert.vinted_search_url, Category.slug)
                .join(Category, Alert.category_id == Category.id)
                .where(
                    Alert.status == AlertStatus.ACTIVE,
                    Alert.deleted_at.is_(None),
                    Alert.vinted_search_url.is_not(None),
                    Category.slug != CategorySlug.BD_COMICS.value,
                )
            )
            result = await session.execute(stmt)
            return dedupe_custom_searches(
                [(url, cat_slug) for url, cat_slug in result.all() if url]
            )
    except Exception as exc:
        logger.warning("Impossible de charger les recherches personnalisées : %s", exc)
        return []


CUSTOM_SEARCH_TTL_SECONDS = 60.0
_custom_searches_cache: tuple[float, list[tuple[str, str]]] | None = None


async def get_active_custom_searches_cached(now: float | None = None) -> list[tuple[str, str]]:
    """Recharge les recherches personnalisées au plus une fois par TTL."""
    global _custom_searches_cache
    current = time.time() if now is None else now
    if (
        _custom_searches_cache is not None
        and current - _custom_searches_cache[0] < CUSTOM_SEARCH_TTL_SECONDS
    ):
        return _custom_searches_cache[1]

    searches = await load_active_custom_searches()
    _custom_searches_cache = (current, searches)
    return searches


def reset_custom_searches_cache() -> None:
    """Vide le cache des recherches (utilisé par les tests)."""
    global _custom_searches_cache
    _custom_searches_cache = None


_ingest_semaphore: asyncio.Semaphore | None = None


def _get_ingest_semaphore() -> asyncio.Semaphore:
    global _ingest_semaphore
    if _ingest_semaphore is None:
        _ingest_semaphore = asyncio.Semaphore(3)
    return _ingest_semaphore


@dataclass(frozen=True)
class ScrapedIngestResult:
    """Résultat d'une ingestion lancée par le poller."""

    notified_count: int
    needs_retry: bool


async def _ingest_scraped_item(item: Any, category_slug: str) -> ScrapedIngestResult:
    """Ingère une annonce dans le processus, sans HTTP localhost."""
    from app.schemas.listing import ListingIngestPayload
    from app.use_cases.ingest_listing import ProcessListingUseCase

    discovered_at, collection_id = latency.discovery()
    timing = latency.AlertTiming(
        ingestion_id=uuid.uuid4(), collection_id=collection_id, discovered_at=discovered_at
    )
    payload_dict = to_ingest_payload(item, category_slug=category_slug)
    payload = ListingIngestPayload.model_validate(payload_dict)
    waiting_at = latency.clock()
    acquired = False
    with latency.timing_context(timing):
        try:
            async with _get_ingest_semaphore():
                acquired = True
                latency.emit(
                    timing, "semaphore_wait", waiting_at, latency.clock(), result="acquired"
                )
                async with AsyncSessionLocal() as session:
                    use_case = ProcessListingUseCase(session)
                    response = await use_case.execute(payload, raw_payload=payload_dict)
                    notified_count = sum(1 for match in response.matches if match.notification_sent)
                    return ScrapedIngestResult(
                        notified_count=notified_count, needs_retry=response.needs_retry
                    )
        except BaseException as exc:
            if not acquired:
                latency.emit(
                    timing,
                    "semaphore_wait",
                    waiting_at,
                    latency.clock(),
                    result="cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                    error_type=type(exc).__name__,
                )
            raise


def _log_window_coverage(received: int, category_slug: str, cycle_index: int) -> None:
    """Journalise la fenêtre réellement obtenue, pour qu'on puisse la mesurer.

    La taille demandée est une intention, pas une garantie : l'API peut plafonner sans
    erreur, et l'ignorer revient à croire que la couverture est alors de plusieurs
    minutes alors qu'elle peut rester à quelques dizaines de secondes. Un cycle sans
    nouveauté n'était journalisé nulle part — c'est précisément le cycle muet qui
    laisserait une annonce passer sous la fenêtre sans trace.

    Le cas tronqué est donc un avertissement, à chaque cycle : il se répète tant qu'il
    dure. Le cas nominal ne sort qu'un heartbeat, sinon deux lignes toutes les 15 s.
    """
    if received < CATALOG_PAGE_SIZE:
        logger.warning(
            "Fenêtre catalogue tronquée : %d items reçus pour %d demandés (%s) — "
            "des annonces peuvent passer sous la fenêtre sans être vues",
            received,
            CATALOG_PAGE_SIZE,
            category_slug,
        )
    elif cycle_index % WINDOW_LOG_EVERY_CYCLES == 0:
        logger.info(
            "Fenêtre catalogue : %d items reçus pour %d demandés (%s)",
            received,
            CATALOG_PAGE_SIZE,
            category_slug,
        )


async def poll_vinted_once(
    seen_ids: set[str],
    vinted_client: httpx.AsyncClient,
    target_url: str = DEFAULT_GLOBAL_BD_URL,
    category_slug: str = "bd_comics",
    cycle_index: int = 0,
) -> int:
    """Exécute un cycle de scraping sur une URL cible et pousse les nouvelles annonces.

    Retourne le nombre de nouvelles annonces ingérées avec succès.
    """
    collection_timing = latency.AlertTiming(collection_id=uuid.uuid4())
    scan_started_at = datetime.now(UTC)
    collection_started = latency.clock()
    try:
        items = await fetch_vinted_page(target_url, vinted_client)
    except BaseException as exc:
        latency.emit(collection_timing, "collection", collection_started, latency.clock(),
                     result="cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                     error_type=type(exc).__name__)
        raise
    discovered_at = latency.clock()
    latency.emit(collection_timing, "collection", collection_started, discovered_at,
                 result="returned", item_count=len(items))
    async with AsyncSessionLocal() as baseline_session:
        await AlertRepository(baseline_session).initialize_listing_baselines(
            category_slug, target_url, [item.external_id for item in items], scan_started_at,
        )
        await baseline_session.commit()
    _log_window_coverage(len(items), category_slug, cycle_index)
    if not items:
        return 0

    new_items = [item for item in items if item.external_id not in seen_ids]
    if not new_items:
        return 0

    logger.info(
        "Vinted poller : %d nouvelles annonces détectées sur %s (%s)",
        len(new_items),
        target_url[:60],
        category_slug,
    )
    for item in new_items:
        seen_ids.add(item.external_id)

    with latency.discovery_context(discovered_at, collection_timing.collection_id):
        results = await asyncio.gather(
            *(_ingest_scraped_item(item, category_slug) for item in new_items),
            return_exceptions=True,
        )
    ingested_count = 0
    for item, result in zip(new_items, results, strict=True):
        if isinstance(result, BaseException):
            seen_ids.discard(item.external_id)
            if not isinstance(result, asyncio.CancelledError):
                logger.warning(
                    "Erreur lors de l'ingestion de l'annonce %s : %s",
                    item.external_id,
                    result,
                )
            continue
        if result.needs_retry:
            seen_ids.discard(item.external_id)
        ingested_count += 1
        if result.notified_count:
            logger.info(
                "MATCH VINTED ! Titre: '%s' (Prix: %.2f €) -> %d alertes notifiées !",
                item.title,
                item.item_price_cents / 100,
                result.notified_count,
            )
    return ingested_count


async def run_vinted_poller(stop_event: asyncio.Event) -> None:
    """Boucle principale de surveillance Vinted haute performance."""
    if pilot_mode.active():
        return
    if not settings.VINTED_POLLER_ENABLED:
        logger.info("Vinted poller désactivé dans la configuration.")
        return

    logger.info(
        "Démarrage du Vinted Poller haute fréquence (intervalle: %ds, ingest in-process)...",
        settings.VINTED_POLL_INTERVAL_SECONDS,
    )

    seen_ids: FifoSet = await load_initial_seen_ids()
    logger.info("Vinted poller initialisé avec %d IDs en mémoire.", len(seen_ids))

    if settings.VINTED_PROXY_URL and not settings.VINTED_PROXY_ENABLED:
        logger.warning(
            "VINTED_PROXY_URL est configurée mais VINTED_PROXY_ENABLED est faux : proxy ignoré."
        )
    proxy_url = settings.get_vinted_proxy()
    if proxy_url:
        logger.info("Vinted poller configuré avec proxy actif.")

    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        proxy=proxy_url,
    ) as vinted_client:
        consecutive_blocks = 0
        poll_cycle = 0
        cadence = CycleCadence()
        availability = AvailabilityLedger()
        consecutive_collection_failures = 0
        collection_failure_notified = False
        night_active = False
        night_started_at = 0.0
        search_first_seen: dict[tuple[str, str], float] = {}
        search_last_success: dict[tuple[str, str], float] = {}

        while not stop_event.is_set():
            if is_night_time():
                night_started = time.monotonic()
                remaining = get_night_mode_remaining_seconds()
                if not night_active:
                    night_active = True
                    night_started_at = night_started
                    logger.info(
                        "Veille de nuit : entrée de la suspension (reprise dans %.0f min).",
                        remaining / 60.0,
                        extra={
                            "event": "poller_night",
                            "phase": "start",
                            "remaining_s": round(remaining, 1),
                        },
                    )
                hours_left = int(remaining // 3600)
                mins_left = int((remaining % 3600) // 60)
                logger.info(
                    "🌙 MODE NUIT ACTIF (%02d:00 - %02d:00 %s) : Suspension de la surveillance "
                    "Vinted pendant encore %dh%02dm pour préserver l'IP et respecter le calme "
                    "nocturne.",
                    settings.NIGHT_MODE_START_HOUR,
                    settings.NIGHT_MODE_END_HOUR,
                    settings.NIGHT_MODE_TIMEZONE,
                    hours_left,
                    mins_left,
                )
                await save_poller_status({
                    "status": "night_mode",
                    "consecutive_blocks": 0,
                    "cooldown_until": None,
                    "last_poll_at": time.time(),
                    "wake_up_hour": settings.NIGHT_MODE_END_HOUR,
                    "availability": availability.snapshot(),
                    "message": (
                        "Mode Nuit actif (veille jusqu'à "
                        f"{settings.NIGHT_MODE_END_HOUR:02d}:00)"
                    ),
                })
                await vinted_session_manager.clear_session(vinted_client)
                while is_night_time() and not stop_event.is_set():
                    sleep_chunk = min(60.0, max(5.0, get_night_mode_remaining_seconds()))
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop_event.wait(), timeout=sleep_chunk)

                if stop_event.is_set():
                    break

                logger.info(
                    "☀️ FIN DU MODE NUIT : Reprise de la surveillance Vinted "
                    "haute performance."
                )
                consecutive_blocks = 0
                consecutive_collection_failures = 0
                await asyncio.sleep(random.uniform(2.0, 5.0))
                availability.add("night", time.monotonic() - night_started)
                continue

            if night_active:
                night_active = False
                night_span = time.monotonic() - night_started_at
                logger.info(
                    "Veille de nuit : sortie de la suspension (%.0f min couvertes).",
                    night_span / 60.0,
                    extra={
                        "event": "poller_night",
                        "phase": "end",
                        "duration_s": round(night_span, 1),
                    },
                )

            poll_cycle += 1
            cycle_started = time.monotonic()
            outcome = "scanned"
            count_bd = 0
            count_custom = 0
            bd_scan_ok = False
            requests_before = vinted_request_stats.totals()
            try:
                count_bd = await poll_vinted_once(
                    seen_ids=seen_ids,
                    vinted_client=vinted_client,
                    target_url=DEFAULT_GLOBAL_BD_URL,
                    category_slug=CategorySlug.BD_COMICS.value,
                    cycle_index=poll_cycle,
                )
                bd_scan_ok = True

                custom_searches = await get_active_custom_searches_cached()
                now_monotonic = time.monotonic()
                current_search_keys = set(custom_searches)
                for stale_key in set(search_first_seen) - current_search_keys:
                    search_first_seen.pop(stale_key, None)
                    search_last_success.pop(stale_key, None)
                for new_key in current_search_keys - set(search_first_seen):
                    search_first_seen[new_key] = now_monotonic
                if custom_searches and not stop_event.is_set():
                    custom_url, custom_slug = custom_searches[poll_cycle % len(custom_searches)]
                    await asyncio.sleep(random.uniform(1.2, 2.0))
                    count_custom = await poll_vinted_once(
                        seen_ids=seen_ids,
                        vinted_client=vinted_client,
                        target_url=custom_url,
                        category_slug=custom_slug,
                        cycle_index=poll_cycle,
                    )
                    search_last_success[(custom_url, custom_slug)] = time.monotonic()

                if consecutive_blocks > 0:
                    logger.info(
                        "🛡️ Garde anti-blocage : Connexion Vinted rétablie avec succès (IP propre).")
                    consecutive_blocks = 0
                    safe_create_task(
                        notify_admins(
                            text=(
                                "✅ <b>BotRadar — Surveillance Vinted rétablie</b>\n\n"
                                "🟢 La connexion à Vinted est de nouveau opérationnelle !\n"
                                "Le scan des nouvelles annonces a repris normalement."
                            )
                        ),
                        name="notify_vinted_recovered",
                    )

                consecutive_collection_failures = 0
                if collection_failure_notified:
                    collection_failure_notified = False
                    safe_create_task(
                        notify_admins(
                            text=(
                                "✅ <b>BotRadar — Collecte Vinted rétablie</b>\n\n"
                                "🟢 Les cycles de scrutation aboutissent de nouveau."
                            )
                        ),
                        name="notify_vinted_collection_recovered",
                    )

                now_ts = time.time()
                cadence.record(now_ts)

                await save_poller_status({
                    "status": "active",
                    "consecutive_blocks": 0,
                    "cooldown_until": None,
                    "cooldown_seconds": 0,
                    "last_poll_at": now_ts,
                    "last_items_found": count_bd + count_custom,
                    "cycle_gap_p50_s": cadence.percentile(50),
                    "cycle_gap_p90_s": cadence.percentile(90),
                    "availability": availability.snapshot(),
                    "requests_60s": vinted_request_stats.window_counts(),
                    "search_stats": search_freshness_snapshot(
                        search_first_seen, search_last_success, time.monotonic()
                    ),
                    "message": "Surveillance Vinted active",
                })

                if len(seen_ids) > seen_ids.max_size:
                    seen_ids.trim()

            except (VintedBlockError, VintedRateLimitError) as block_exc:
                consecutive_blocks += 1
                cooldown_map = {1: 180, 2: 420, 3: 900, 4: 1800}
                cooldown = cooldown_map.get(consecutive_blocks, 3600)
                cooldown_until = time.time() + cooldown
                cooldown_mins = cooldown // 60

                try:
                    tz = ZoneInfo(settings.NIGHT_MODE_TIMEZONE)
                except Exception:
                    tz = ZoneInfo("Europe/Paris")
                resume_time_str = datetime.fromtimestamp(cooldown_until, tz).strftime("%H:%M")
                reason_str = (
                    f"Blocage Vinted (403, indice: {block_exc.origin_hint})"
                    if isinstance(block_exc, VintedBlockError)
                    else "Rate Limit Vinted (429)"
                )

                logger.warning(
                    "🛡️ GARDE ANTI-BLOCAGE ACTIF : %s détecté (#%d). "
                    "Mise en pause de refroidissement pendant %d s (%d min) pour préserver l'IP...",
                    block_exc,
                    consecutive_blocks,
                    cooldown,
                    cooldown_mins,
                )

                await save_poller_status({
                    "status": "cooldown",
                    "consecutive_blocks": consecutive_blocks,
                    "cooldown_until": cooldown_until,
                    "cooldown_seconds": cooldown,
                    "last_poll_at": time.time(),
                    "availability": availability.snapshot(),
                    "reason": reason_str,
                    "resume_time": resume_time_str,
                    "message": f"Pause de sécurité #{consecutive_blocks} ({cooldown_mins} min)",
                })

                await vinted_session_manager.clear_session(vinted_client)

                safe_create_task(
                    notify_admins(
                        text=(
                            f"🛡️ <b>Alerte BotRadar — Pause de sécurité</b>\n\n"
                            f"⚠️ <b>{reason_str}</b> détecté sur Vinted.\n"
                            "Mise en pause de protection de l'IP : "
                            f"<b>Palier #{consecutive_blocks} "
                            f"({cooldown_mins} min)</b>.\n\n"
                            f"⏱ <b>Reprise prévue à</b> : {resume_time_str}\n"
                            "<i>Le bot tentera automatiquement de se "
                            "reconnecter à l'heure indiquée.</i>"
                        )
                    ),
                    name="notify_vinted_blocked",
                )

                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=cooldown)
                blocked_until = time.monotonic()
                availability.add("blocked", blocked_until - cycle_started, blocked_until)
                _log_cycle_journal(
                    poll_cycle,
                    "blocked",
                    blocked_until - cycle_started,
                    None,
                    0,
                    _request_delta(requests_before),
                )
                continue

            except VintedCollectionError as exc:
                outcome = "network_error"
                if bd_scan_ok:
                    logger.warning(
                        "Collecte Vinted échouée sur une recherche personnalisée : %s",
                        type(exc).__name__,
                    )
                else:
                    consecutive_collection_failures += 1
                    logger.warning(
                        "Collecte Vinted échouée (%d cycles consécutifs) : %s",
                        consecutive_collection_failures,
                        type(exc).__name__,
                    )
                    if (
                        consecutive_collection_failures == COLLECTION_FAILURE_ALERT_THRESHOLD
                        and not collection_failure_notified
                    ):
                        collection_failure_notified = True
                        safe_create_task(
                            notify_admins(
                                text=(
                                    "⚠️ <b>BotRadar — Collecte Vinted dégradée</b>\n\n"
                                    f"⚠️ {consecutive_collection_failures} cycles consécutifs "
                                    "sans collecte du flux Livres (réseau ou API).\n"
                                    "Le bot poursuit ses cycles sans pause anti-blocage."
                                )
                            ),
                            name="notify_vinted_collection_degraded",
                        )

            except Exception as exc:
                outcome = "unexpected_error"
                logger.error("Erreur inattendue dans la boucle du Vinted Poller: %s", exc)

            target_interval = get_adaptive_poll_interval()
            jitter = random.uniform(-2.5, 3.5)
            sleep_time = max(MIN_CYCLE_GAP_SECONDS, target_interval + jitter)

            work_seconds = time.monotonic() - cycle_started
            sleep_time = remaining_cycle_sleep(sleep_time, work_seconds)

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_time)

            cycle_ended = time.monotonic()
            availability.add(outcome, cycle_ended - cycle_started, cycle_ended)
            _log_cycle_journal(
                poll_cycle,
                outcome,
                cycle_ended - cycle_started,
                target_interval,
                count_bd + count_custom,
                _request_delta(requests_before),
            )

        await save_poller_status({
            "status": "stopped",
            "last_poll_at": time.time(),
            "message": "Surveillance Vinted arrêtée",
        })

    logger.info("Vinted Poller arrêté proprement.")
