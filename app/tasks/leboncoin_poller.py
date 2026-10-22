"""Tâche d'arrière-plan de collecte Leboncoin : flux BD, sortie résidentielle.

Le poller interroge une page de recherche unique (catégorie Livres filtrée sur le
genre BD/manga), triée par nouveauté par Leboncoin, puis ingère les annonces dans
le même processus que l'API. Aucun appel réseau n'est fait avant la décision
Telegram : la collecte précède l'ingestion, comme pour Vinted.
"""

import asyncio
import contextlib
import json
import logging
import random
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.core.constants import LEBONCOIN_SOURCE_NAME, CategorySlug
from app.core.redis import get_redis_client
from app.core.tasks import safe_create_task
from app.db.repositories.listing_repo import ListingRepository
from app.db.session import AsyncSessionLocal
from app.services.leboncoin_parsing import (
    ScrapedLeboncoinItem,
    parse_search_page,
    to_ingest_payload,
)
from app.services.leboncoin_scraper import (
    LeboncoinBlockError,
    LeboncoinCollectionError,
    fetch_search_page,
    reset_session,
)
from app.services.telegram_service import notify_admins
from app.tasks.vinted_poller import FifoSet, remaining_cycle_sleep

logger = logging.getLogger("radar.leboncoin_poller")

LEBONCOIN_POLLER_STATUS_KEY = "radar:leboncoin_poller_status"

MIN_CYCLE_GAP_SECONDS = 20.0
WINDOW_LOG_EVERY_CYCLES = 20
COLLECTION_FAILURE_ALERT_THRESHOLD = 5
BLOCK_COOLDOWNS = (600, 1800, 3600, 7200)
MAX_COLLECTION_BACKOFF_SECONDS = 1800.0
SATURATION_MIN_ADS = 30
SILENCE_ALERT_SECONDS = 900

_ingest_semaphore: asyncio.Semaphore | None = None


def _get_ingest_semaphore() -> asyncio.Semaphore:
    global _ingest_semaphore
    if _ingest_semaphore is None:
        _ingest_semaphore = asyncio.Semaphore(3)
    return _ingest_semaphore


@dataclass(frozen=True)
class IngestOutcome:
    """Résultat d'une ingestion lancée par le poller."""

    notified_count: int
    needs_retry: bool
    ingested_count: int = 0
    ads_seen: int = 0
    ads_since_last_cycle: int | None = None
    pages_fetched: int = 1
    baseline_established: bool = False


async def save_poller_status(data: dict[str, Any]) -> None:
    """Enregistre l'état du poller dans Redis (TTL 24 h)."""
    try:
        client = get_redis_client()
        await client.set(LEBONCOIN_POLLER_STATUS_KEY, json.dumps(data), ex=86400)
    except Exception as exc:
        logger.debug("Impossible d'enregistrer l'état Leboncoin dans Redis : %s", exc)


async def get_poller_status() -> dict[str, Any] | None:
    """Récupère l'état du poller Leboncoin depuis Redis."""
    try:
        client = get_redis_client()
        raw = await client.get(LEBONCOIN_POLLER_STATUS_KEY)
        if raw:
            data: dict[str, Any] = json.loads(raw)
            return data
    except Exception as exc:
        logger.debug("Impossible de lire l'état Leboncoin depuis Redis : %s", exc)
    return None


async def load_initial_seen_ids() -> FifoSet:
    """Recharge les annonces Leboncoin récentes, sans masquer une notification à envoyer."""
    try:
        async with AsyncSessionLocal() as session:
            raw_ids = await ListingRepository(session).get_initial_seen_external_ids(
                LEBONCOIN_SOURCE_NAME
            )
            return FifoSet(reversed(raw_ids))
    except Exception as exc:
        logger.warning("Impossible de charger les anciens IDs Leboncoin : %s", exc)
        return FifoSet()


async def _source_is_empty() -> bool:
    """Vrai seulement si aucune annonce Leboncoin n'existe en base."""
    try:
        async with AsyncSessionLocal() as session:
            count = await ListingRepository(session).count_by_source(LEBONCOIN_SOURCE_NAME)
            return count == 0
    except Exception as exc:
        logger.warning("Impossible de compter les annonces Leboncoin : %s", exc)
        return False


async def _fetch_items(
    url: str,
    proxy_url: str | None,
    page: int = 1,
) -> list[ScrapedLeboncoinItem]:
    """Une requête HTTP hors boucle d'événements : curl_cffi est synchrone."""
    html = await asyncio.to_thread(fetch_search_page, url, proxy_url, page)
    return parse_search_page(html)


async def _ingest_item(item: ScrapedLeboncoinItem, category_slug: str) -> IngestOutcome:
    """Ingère une annonce dans le processus, sans HTTP localhost."""
    from app.schemas.listing import ListingIngestPayload
    from app.use_cases.ingest_listing import ProcessListingUseCase

    payload_dict = to_ingest_payload(item, category_slug=category_slug)
    payload = ListingIngestPayload.model_validate(payload_dict)
    async with _get_ingest_semaphore(), AsyncSessionLocal() as session:
        response = await ProcessListingUseCase(session).execute(
            payload, raw_payload=payload_dict
        )
    notified_count = sum(1 for match in response.matches if match.notification_sent)
    return IngestOutcome(notified_count=notified_count, needs_retry=response.needs_retry)


def _log_window(
    items: list[ScrapedLeboncoinItem],
    cycle_index: int,
    ads_since_last_cycle: int | None = None,
) -> None:
    """Journalise la fenêtre obtenue et sa part réellement nouvelle."""
    if not items:
        logger.warning("Page Leboncoin vide : aucune annonce extraite.")
        return
    since_label = (
        f", dont {ads_since_last_cycle} publiées depuis le cycle précédent"
        if ads_since_last_cycle is not None
        else ""
    )
    if len(items) < _expected_page_size():
        logger.warning(
            "Fenêtre Leboncoin courte : %d annonces pour %d attendues (%s)%s",
            len(items),
            _expected_page_size(),
            LEBONCOIN_SOURCE_NAME,
            since_label,
        )
    elif cycle_index % WINDOW_LOG_EVERY_CYCLES == 0:
        logger.info(
            "Fenêtre Leboncoin : %d annonces extraites%s",
            len(items),
            since_label,
        )


def _expected_page_size() -> int:
    """Taille nominale d'une page de résultats Leboncoin."""
    return 35


def _count_since(
    items: list[ScrapedLeboncoinItem],
    since: datetime | None,
) -> int | None:
    """Annonces publiées depuis le cycle précédent ; None au premier passage."""
    if since is None:
        return None
    return sum(
        1
        for item in items
        if item.published_at is not None and item.published_at >= since
    )


def _previous_poll_instant(status: dict[str, Any] | None) -> float | None:
    """Horodatage du cycle précédent, s'il est exploitable."""
    if not status:
        return None
    value = status.get("last_poll_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _needs_second_page(
    items: list[ScrapedLeboncoinItem],
    ads_since_last_cycle: int | None,
) -> bool:
    """Page 2 utile si la page 1 est saturée de nouveautés ou entièrement antérieure au cycle."""
    if settings.LEBONCOIN_CATCHUP_MAX_PAGES <= 1 or ads_since_last_cycle is None:
        return False
    if ads_since_last_cycle >= SATURATION_MIN_ADS:
        return True
    return ads_since_last_cycle == 0 and len(items) >= _expected_page_size()


async def poll_leboncoin_once(
    seen_ids: set[str],
    cycle_index: int = 0,
    *,
    baseline: bool = False,
    since: datetime | None = None,
) -> IngestOutcome:
    """Un cycle : collecte, rattrapage borné, filtre des annonces déjà vues, ingestion."""
    url = settings.LEBONCOIN_SEARCH_URL
    proxy_url = settings.get_leboncoin_proxy()
    items = await _fetch_items(url, proxy_url, 1)
    pages_fetched = 1
    ads_since_last_cycle = _count_since(items, since)

    if not baseline and _needs_second_page(items, ads_since_last_cycle):
        known = {item.external_id for item in items}
        try:
            next_page = await _fetch_items(url, proxy_url, 2)
        except LeboncoinCollectionError as exc:
            logger.warning(
                "Page 2 Leboncoin indisponible (%s) : %d annonces de page 1 conservées.",
                exc,
                len(items),
            )
        else:
            pages_fetched = 2
            items = [*items, *(item for item in next_page if item.external_id not in known)]
            logger.info(
                "Rattrapage Leboncoin (%d nouvelles, %d annonces en page 1) : page 2 lue.",
                ads_since_last_cycle or 0,
                len(known),
            )

    _log_window(items, cycle_index, ads_since_last_cycle)
    outcome = IngestOutcome(
        notified_count=0,
        needs_retry=False,
        ads_seen=len(items),
        ads_since_last_cycle=ads_since_last_cycle,
        pages_fetched=pages_fetched,
    )
    if not items:
        return outcome

    if baseline:
        seen_ids.update(item.external_id for item in items)
        logger.info(
            "Référence Leboncoin posée : %d annonces marquées vues sans notification.",
            len(items),
        )
        return replace(outcome, baseline_established=True)

    new_items = [item for item in items if item.external_id not in seen_ids]
    if not new_items:
        return outcome

    logger.info(
        "Leboncoin : %d nouvelles annonces détectées (%s)",
        len(new_items),
        url[:80],
    )
    for item in new_items:
        seen_ids.add(item.external_id)

    results = await asyncio.gather(
        *(_ingest_item(item, CategorySlug.BD_COMICS.value) for item in new_items),
        return_exceptions=True,
    )
    ingested_count = 0
    notified_count = 0
    needs_retry = False
    for item, result in zip(new_items, results, strict=True):
        if isinstance(result, BaseException):
            seen_ids.discard(item.external_id)
            if not isinstance(result, asyncio.CancelledError):
                logger.warning(
                    "Erreur d'ingestion Leboncoin pour %s : %s", item.external_id, result
                )
            continue
        if result.needs_retry:
            seen_ids.discard(item.external_id)
            needs_retry = True
        ingested_count += 1
        notified_count += result.notified_count
        if result.notified_count:
            logger.info(
                "MATCH LEBONCOIN ! Titre: '%s' (Prix: %.2f €) -> %d alertes notifiées !",
                item.title,
                item.item_price_cents / 100,
                result.notified_count,
            )
    return replace(
        outcome,
        notified_count=notified_count,
        needs_retry=needs_retry,
        ingested_count=ingested_count,
    )


def _block_cooldown(consecutive_blocks: int) -> int:
    """Palier de refroidissement croissant, plafonné au dernier."""
    index = min(consecutive_blocks, len(BLOCK_COOLDOWNS)) - 1
    return BLOCK_COOLDOWNS[max(index, 0)]


async def _alert_silence_once(
    last_success_at: float,
    silence_notified: bool,
) -> bool:
    """Prévient une fois quand aucun cycle exploitable n'a abouti depuis le seuil de silence."""
    if silence_notified:
        return silence_notified
    if time.time() - last_success_at < SILENCE_ALERT_SECONDS:
        return silence_notified
    silent_seconds = int(time.time() - last_success_at)
    logger.error(
        "Collecte Leboncoin muette depuis %d s : alerte de silence émise.",
        silent_seconds,
    )
    message = (
        "🚨 <b>BotRadar — Collecte Leboncoin muette</b>\n\n"
        f"🚨 Aucun cycle exploitable depuis {silent_seconds // 60} min.\n"
        "Vérifier le tunnel résidentiel du Pi et son relais SOCKS5."
    )
    safe_create_task(notify_admins(text=message), name="notify_leboncoin_silence")
    return True


async def run_leboncoin_poller(stop_event: asyncio.Event) -> None:
    """Boucle de collecte Leboncoin, cadencée et tolérante aux blocages."""
    if not settings.LEBONCOIN_POLLER_ENABLED:
        logger.info("Collecte Leboncoin désactivée dans la configuration.")
        return

    logger.info(
        "Démarrage du poller Leboncoin (intervalle : %ds, ingestion in-process).",
        settings.LEBONCOIN_POLL_INTERVAL_SECONDS,
    )

    seen_ids: FifoSet = await load_initial_seen_ids()
    logger.info("Poller Leboncoin initialisé avec %d IDs en mémoire.", len(seen_ids))

    status = await get_poller_status()
    baseline_pending = status is None and await _source_is_empty()
    if baseline_pending:
        logger.info(
            "Aucune annonce Leboncoin connue : la première page servira de référence "
            "sans notification."
        )
    last_poll_instant = _previous_poll_instant(status)
    baseline_at: float | None = None

    if settings.LEBONCOIN_PROXY_URL and not settings.LEBONCOIN_PROXY_ENABLED:
        logger.warning(
            "LEBONCOIN_PROXY_URL est configurée mais LEBONCOIN_PROXY_ENABLED est faux : "
            "proxy ignoré."
        )
    if settings.get_leboncoin_proxy() is None:
        logger.warning(
            "Collecte Leboncoin sans proxy : depuis un datacenter, DataDome répond 403."
        )

    consecutive_blocks = 0
    consecutive_collection_failures = 0
    collection_failure_notified = False
    silence_notified = False
    last_success_at = time.time()
    poll_cycle = 0

    while not stop_event.is_set():
        poll_cycle += 1
        cycle_started = time.monotonic()
        try:
            since = (
                datetime.fromtimestamp(last_poll_instant, tz=UTC)
                if last_poll_instant is not None and not baseline_pending
                else None
            )
            outcome = await poll_leboncoin_once(
                seen_ids=seen_ids,
                cycle_index=poll_cycle,
                baseline=baseline_pending,
                since=since,
            )
            if outcome.baseline_established:
                baseline_pending = False
                baseline_at = time.time()

            if consecutive_blocks > 0:
                logger.info("Connexion Leboncoin rétablie.")
                consecutive_blocks = 0
                safe_create_task(
                    notify_admins(
                        text=(
                            "✅ <b>BotRadar — Collecte Leboncoin rétablie</b>\n\n"
                            "🟢 Les pages de recherche sont de nouveau accessibles."
                        )
                    ),
                    name="notify_leboncoin_recovered",
                )

            consecutive_collection_failures = 0
            last_success_at = time.time()
            silence_notified = False
            if collection_failure_notified:
                collection_failure_notified = False
                safe_create_task(
                    notify_admins(
                        text=(
                            "✅ <b>BotRadar — Collecte Leboncoin rétablie</b>\n\n"
                            "🟢 Les cycles de collecte aboutissent de nouveau."
                        )
                    ),
                    name="notify_leboncoin_collection_recovered",
                )

            last_poll_instant = time.time()
            await save_poller_status({
                "status": "active",
                "consecutive_blocks": 0,
                "last_poll_at": last_poll_instant,
                "last_ingested": outcome.ingested_count,
                "ads_seen": outcome.ads_seen,
                "ads_since_last_cycle": outcome.ads_since_last_cycle,
                "pages_fetched": outcome.pages_fetched,
                "baseline_at": baseline_at,
                "seen_ids": len(seen_ids),
                "message": (
                    "Référence initiale posée sans notification"
                    if outcome.baseline_established
                    else "Collecte Leboncoin active"
                ),
            })

        except LeboncoinBlockError as block_exc:
            consecutive_blocks += 1
            cooldown = _block_cooldown(consecutive_blocks)
            reset_session()
            logger.warning(
                "DataDome refuse la collecte Leboncoin (#%d, %s) : pause de %d s.",
                consecutive_blocks,
                block_exc,
                cooldown,
            )
            await save_poller_status({
                "status": "cooldown",
                "consecutive_blocks": consecutive_blocks,
                "last_poll_at": time.time(),
                "cooldown_seconds": cooldown,
                "message": f"Pause de sécurité #{consecutive_blocks} ({cooldown // 60} min)",
            })
            safe_create_task(
                notify_admins(
                    text=(
                        "🛡️ <b>Alerte BotRadar — Leboncoin bloqué</b>\n\n"
                        f"⚠️ DataDome a refusé la collecte ({block_exc}).\n"
                        f"Mise en pause : <b>palier #{consecutive_blocks} "
                        f"({cooldown // 60} min)</b>.\n\n"
                        "<i>La collecte reprendra automatiquement.</i>"
                    )
                ),
                name="notify_leboncoin_blocked",
            )
            silence_notified = await _alert_silence_once(last_success_at, silence_notified)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=cooldown)
            continue

        except LeboncoinCollectionError as exc:
            consecutive_collection_failures += 1
            backoff = min(
                60.0 * 2 ** (consecutive_collection_failures - 1),
                MAX_COLLECTION_BACKOFF_SECONDS,
            )
            logger.warning(
                "Collecte Leboncoin échouée (%d cycles consécutifs) : %s",
                consecutive_collection_failures,
                exc,
            )
            if (
                consecutive_collection_failures == COLLECTION_FAILURE_ALERT_THRESHOLD
                and not collection_failure_notified
            ):
                collection_failure_notified = True
                safe_create_task(
                    notify_admins(
                        text=(
                            "⚠️ <b>BotRadar — Collecte Leboncoin dégradée</b>\n\n"
                            f"⚠️ {consecutive_collection_failures} cycles consécutifs "
                            "sans collecte (réseau, tunnel ou structure de page)."
                        )
                    ),
                    name="notify_leboncoin_collection_degraded",
                )
            silence_notified = await _alert_silence_once(last_success_at, silence_notified)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            continue

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            logger.error("Erreur inattendue dans la boucle Leboncoin : %s", exc)

        target_interval = float(settings.LEBONCOIN_POLL_INTERVAL_SECONDS)
        jitter = random.uniform(-0.1 * target_interval, 0.15 * target_interval)
        sleep_time = max(MIN_CYCLE_GAP_SECONDS, target_interval + jitter)
        work_seconds = time.monotonic() - cycle_started
        sleep_time = remaining_cycle_sleep(sleep_time, work_seconds)

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_time)

    await save_poller_status({
        "status": "stopped",
        "last_poll_at": time.time(),
        "message": "Collecte Leboncoin arrêtée",
    })
    logger.info("Poller Leboncoin arrêté proprement.")
