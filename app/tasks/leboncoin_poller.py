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
from dataclasses import dataclass
from datetime import datetime
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


async def _fetch_items(
    url: str,
    proxy_url: str | None,
) -> list[ScrapedLeboncoinItem]:
    """Une requête HTTP hors boucle d'événements : curl_cffi est synchrone."""
    html = await asyncio.to_thread(fetch_search_page, url, proxy_url)
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


def _log_window(items: list[ScrapedLeboncoinItem], cycle_index: int) -> None:
    """Journalise la fenêtre réellement obtenue : une page courte rate des annonces."""
    if not items:
        logger.warning("Page Leboncoin vide : aucune annonce extraite.")
        return
    dated = [item.published_at for item in items if item.published_at is not None]
    oldest_age = ""
    if dated:
        age_minutes = (datetime.now(tz=dated[0].tzinfo) - min(dated)).total_seconds() / 60
        oldest_age = f", plus ancienne il y a {age_minutes:.0f} min"
    if len(items) < _expected_page_size():
        logger.warning(
            "Fenêtre Leboncoin courte : %d annonces pour %d attendues (%s)%s",
            len(items),
            _expected_page_size(),
            LEBONCOIN_SOURCE_NAME,
            oldest_age,
        )
    elif cycle_index % WINDOW_LOG_EVERY_CYCLES == 0:
        logger.info(
            "Fenêtre Leboncoin : %d annonces extraites%s",
            len(items),
            oldest_age,
        )


def _expected_page_size() -> int:
    """Taille nominale d'une page de résultats Leboncoin."""
    return 35


async def poll_leboncoin_once(
    seen_ids: set[str],
    cycle_index: int = 0,
) -> int:
    """Un cycle : collecte, filtre des annonces déjà vues, ingestion."""
    url = settings.LEBONCOIN_SEARCH_URL
    proxy_url = settings.get_leboncoin_proxy()
    items = await _fetch_items(url, proxy_url)
    _log_window(items, cycle_index)
    if not items:
        return 0

    new_items = [item for item in items if item.external_id not in seen_ids]
    if not new_items:
        return 0

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
        ingested_count += 1
        if result.notified_count:
            logger.info(
                "MATCH LEBONCOIN ! Titre: '%s' (Prix: %.2f €) -> %d alertes notifiées !",
                item.title,
                item.item_price_cents / 100,
                result.notified_count,
            )
    return ingested_count


def _block_cooldown(consecutive_blocks: int) -> int:
    """Palier de refroidissement croissant, plafonné au dernier."""
    index = min(consecutive_blocks, len(BLOCK_COOLDOWNS)) - 1
    return BLOCK_COOLDOWNS[max(index, 0)]


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
    poll_cycle = 0

    while not stop_event.is_set():
        poll_cycle += 1
        cycle_started = time.monotonic()
        try:
            count = await poll_leboncoin_once(seen_ids=seen_ids, cycle_index=poll_cycle)

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

            await save_poller_status({
                "status": "active",
                "consecutive_blocks": 0,
                "last_poll_at": time.time(),
                "last_items_found": count,
                "seen_ids": len(seen_ids),
                "message": "Collecte Leboncoin active",
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
