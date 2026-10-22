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
import math
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
    LeboncoinParseError,
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
LEBONCOIN_BLOCK_EPISODE_KEY = "radar:leboncoin_block_episode"

MIN_CYCLE_GAP_SECONDS = 20.0
WINDOW_LOG_EVERY_CYCLES = 20
COLLECTION_FAILURE_ALERT_THRESHOLD = 5
EXPECTED_PAGE_SIZE = 35
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


class BlockEpisodeError(RuntimeError):
    """L'état d'épisode de blocage est illisible, invalide ou impossible à persister."""


@dataclass(frozen=True)
class BlockEpisode:
    """Épisode de blocage DataDome : ouverture, deadline et arrêt de sécurité."""

    started_at: float
    stopped_at: float | None = None

    def deadline(self) -> float:
        return self.started_at + settings.LEBONCOIN_BLOCK_STOP_SECONDS

    def remaining(self, now: float) -> float:
        return max(0.0, self.deadline() - now)


def _episode_timestamp(raw: Any, field: str) -> float:
    """Convertit une date d'épisode : type, finitude et plausibilité vérifiés."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise BlockEpisodeError(f"{field} d'épisode absente ou non numérique")
    try:
        value = float(raw)
    except (OverflowError, TypeError, ValueError) as exc:
        raise BlockEpisodeError(f"{field} d'épisode non convertible en réel") from exc
    if not math.isfinite(value):
        raise BlockEpisodeError(f"{field} d'épisode non finie")
    if value <= 0:
        raise BlockEpisodeError(f"{field} d'épisode non plausible")
    return value


def _parse_block_episode(raw: Any) -> BlockEpisode | None:
    """Valide un état sérialisé ; une absence est normale, une forme douteuse ne l'est pas."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BlockEpisodeError("état d'épisode non conforme (objet attendu)")
    started_at = _episode_timestamp(raw.get("started_at"), "date de départ")
    raw_stopped_at = raw.get("stopped_at")
    stopped_at: float | None = None
    if raw_stopped_at is not None:
        stopped_at = _episode_timestamp(raw_stopped_at, "date d'arrêt")
        if stopped_at < started_at:
            raise BlockEpisodeError("date d'arrêt antérieure à la date de départ")
    return BlockEpisode(started_at=started_at, stopped_at=stopped_at)


async def get_durable_block_status() -> dict[str, Any] | None:
    """État d'arrêt durable relu depuis l'épisode persistant, sans effet de bord."""
    try:
        episode = await load_block_episode()
    except BlockEpisodeError as exc:
        logger.debug("État d'épisode Leboncoin illisible pour /status : %s", exc)
        return {"status": "durable_block", "reason": "episode_unreadable"}
    if episode is None or episode.stopped_at is None:
        return None
    return {
        "status": "durable_block",
        "reason": "durable_block",
        "stopped_at": episode.stopped_at,
        "started_at": episode.started_at,
    }


async def load_block_episode() -> BlockEpisode | None:
    """Relit l'état d'épisode : absence de clé normale, lecture impossible nommée."""
    try:
        raw = await get_redis_client().get(LEBONCOIN_BLOCK_EPISODE_KEY)
    except Exception as exc:
        raise BlockEpisodeError(f"lecture de l'état d'épisode impossible : {exc}") from exc
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise BlockEpisodeError("état d'épisode illisible (JSON invalide)") from exc
    return _parse_block_episode(decoded)


async def save_block_episode(episode: BlockEpisode) -> None:
    """Écrit l'état d'épisode sans TTL : un arrêt de sécurité n'expire pas de lui-même."""
    payload: dict[str, Any] = {"started_at": episode.started_at}
    if episode.stopped_at is not None:
        payload["stopped_at"] = episode.stopped_at
    try:
        await get_redis_client().set(LEBONCOIN_BLOCK_EPISODE_KEY, json.dumps(payload))
    except Exception as exc:
        raise BlockEpisodeError(f"écriture de l'état d'épisode impossible : {exc}") from exc


async def clear_block_episode() -> None:
    """Efface l'état d'épisode d'un blocage résolu."""
    try:
        await get_redis_client().delete(LEBONCOIN_BLOCK_EPISODE_KEY)
    except Exception as exc:
        raise BlockEpisodeError(f"effacement de l'état d'épisode impossible : {exc}") from exc


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
    return EXPECTED_PAGE_SIZE


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
    if _now() - last_success_at < SILENCE_ALERT_SECONDS:
        return silence_notified
    silent_seconds = int(_now() - last_success_at)
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


async def notify_durable_stop(
    episode: BlockEpisode, persisted: bool, observed_at: float | None = None
) -> None:
    """Annonce la transition vers l'arrêt durable, une seule fois par épisode."""
    elapsed_min = int(max(0.0, (observed_at or _now()) - episode.started_at) // 60)
    threshold_min = int(settings.LEBONCOIN_BLOCK_STOP_SECONDS // 60)
    message = (
        "🛑 <b>BotRadar — Collecte Leboncoin arrêtée</b>\n\n"
        f"⛔ Aucune collecte exploitable depuis <b>{elapsed_min} min</b> "
        f"(seuil d'arrêt : {threshold_min} min).\n"
        "Le collecteur Leboncoin est <b>arrêté</b> ; Vinted, l'API et le bot continuent.\n\n"
        "<i>Reprise manuelle : supprimer la clé d'épisode puis redémarrer le service.</i>"
    )
    if not persisted:
        message = (
            "🛑 <b>BotRadar — Collecte Leboncoin arrêtée</b>\n\n"
            f"⛔ Aucune collecte exploitable depuis <b>{elapsed_min} min</b> "
            f"(seuil d'arrêt : {threshold_min} min).\n"
            "Le collecteur Leboncoin est <b>arrêté pour ce processus</b> ; Vinted, l'API et "
            "le bot continuent.\n\n"
            "⚠️ L'arrêt <b>n'a pas pu être enregistré</b> : après un redémarrage du service, "
            "la collecte peut repartir sans reprise manuelle."
        )
    logger.error("Arrêt durable du collecteur Leboncoin (épisode depuis %s).", episode.started_at)
    safe_create_task(
        notify_admins(text=message),
        name="notify_leboncoin_durable_stop",
    )


def _now() -> float:
    """Horloge murale du collecteur, isolée de la boucle d'événements."""
    return time.time()


def _monotonic() -> float:
    """Horloge monotone du collecteur, isolée de la boucle d'événements."""
    return time.monotonic()


async def _wait_until_stop_or_delay(stop_event: asyncio.Event, timeout: float) -> None:
    """Attend le signal d'arrêt ou l'échéance, sans propager l'expiration."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)


def _bounded_wait(delay: float, episode: BlockEpisode | None) -> float:
    """Plafonne une attente au temps restant de l'épisode ouvert, s'il y en a un."""
    if episode is None:
        return delay
    return max(0.0, min(delay, episode.remaining(_now())))


async def notify_persistence_suspension() -> None:
    """Annonce la suspension du collecteur, sans détail brut de Redis."""
    safe_create_task(
        notify_admins(
            text=(
                "⚠️ <b>BotRadar — Collecte Leboncoin suspendue</b>\n\n"
                "🔒 L'état de blocage n'a pas pu être enregistré : la collecte Leboncoin "
                "est <b>suspendue pour ce processus</b>. Vinted, l'API et le bot continuent.\n\n"
                "⚠️ La non-reprise <b>après redémarrage n'est pas garantie</b> : le collecteur "
                "peut repartir sans reprise manuelle."
            )
        ),
        name="notify_leboncoin_persistence_suspended",
    )


async def _suspend_on_persistence_failure(
    exc: BlockEpisodeError, *, notify: bool = True
) -> None:
    """Suspension du seul Leboncoin quand la protection ne peut pas être persistée."""
    logger.error(
        "Protection d'arrêt Leboncoin non persistée (%s) : collecte suspendue pour "
        "ce processus. La garantie après redémarrage n'est pas assurée.",
        exc,
    )
    if notify:
        await notify_persistence_suspension()
    await save_poller_status({
        "status": "durable_block",
        "reason": "persistence_failed",
        "message": "État de blocage non persistable : collecte Leboncoin suspendue.",
        "detail": str(exc),
    })


async def _conclude_durable_stop(episode: BlockEpisode) -> str:
    """Arrête le collecteur et rend le motif à enregistrer, sans masquer un échec."""
    persisted = await _stop_durably(episode)
    if not persisted:
        await _suspend_on_persistence_failure(
            BlockEpisodeError("arrêt de sécurité non enregistré dans Redis"),
            notify=False,
        )
        return "persistence_failed"
    await save_poller_status({
        "status": "durable_block",
        "reason": "durable_block",
        "stopped_at": _now(),
        "message": "Arrêt durable Leboncoin : reprise manuelle requise.",
    })
    return "durable_block"


async def _stop_durably(episode: BlockEpisode) -> bool:
    """Enregistre l'arrêt de sécurité puis annonce la transition ; rend la persistance."""
    observed_at = _now()
    stopped = BlockEpisode(started_at=episode.started_at, stopped_at=observed_at)
    persisted = True
    try:
        await save_block_episode(stopped)
    except BlockEpisodeError as exc:
        persisted = False
        logger.error("%s : arrêt non garanti après redémarrage.", exc)
    await notify_durable_stop(stopped, persisted, observed_at)
    return persisted


async def run_leboncoin_poller(stop_event: asyncio.Event) -> None:
    """Boucle de collecte Leboncoin, cadencée et tolérante aux blocages."""
    if not settings.LEBONCOIN_POLLER_ENABLED:
        logger.info("Collecte Leboncoin désactivée dans la configuration.")
        return

    logger.info(
        "Démarrage du poller Leboncoin (intervalle : %ds, ingestion in-process).",
        settings.LEBONCOIN_POLL_INTERVAL_SECONDS,
    )

    try:
        episode = await load_block_episode()
    except BlockEpisodeError as exc:
        logger.error(
            "État d'épisode Leboncoin illisible ou invalide (%s) : collecte non démarrée.",
            exc,
        )
        await save_poller_status({
            "status": "durable_block",
            "reason": "episode_unreadable",
            "message": "État d'épisode illisible : collecte Leboncoin suspendue.",
            "detail": str(exc),
        })
        return

    if episode is not None and episode.stopped_at is not None:
        logger.error(
            "Arrêt durable Leboncoin déjà enregistré (%s) : collecte non démarrée.",
            episode.stopped_at,
        )
        await save_poller_status({
            "status": "durable_block",
            "reason": "durable_block",
            "stopped_at": episode.stopped_at,
            "message": "Arrêt durable Leboncoin : reprise manuelle requise.",
        })
        return

    if episode is not None and episode.remaining(_now()) <= 0:
        logger.error(
            "Épisode de blocage échu à l'arrêt du collecteur : arrêt sans nouvelle requête."
        )
        await _conclude_durable_stop(episode)
        return

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
    consecutive_parse_failures = 0
    parse_failure_notified = False
    silence_notified = False
    last_success_at = _now()
    poll_cycle = 0
    durable_block_reason: str | None = None

    while not stop_event.is_set():
        if episode is not None and episode.remaining(_now()) <= 0:
            durable_block_reason = await _conclude_durable_stop(episode)
            break

        poll_cycle += 1
        cycle_started = _monotonic()
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
                baseline_at = _now()

            if episode is not None:
                try:
                    await clear_block_episode()
                except BlockEpisodeError as exc:
                    logger.error(
                        "%s : protection d'arrêt non garantie, collecte suspendue.",
                        exc,
                    )
                    await _suspend_on_persistence_failure(exc)
                    durable_block_reason = "persistence_failed"
                    break
                episode = None

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
            consecutive_parse_failures = 0
            last_success_at = _now()
            silence_notified = False
            if parse_failure_notified:
                parse_failure_notified = False
                safe_create_task(
                    notify_admins(
                        text=(
                            "✅ <b>BotRadar — Collecte Leboncoin rétablie</b>\n\n"
                            "🟢 La page de recherche a de nouveau une structure connue."
                        )
                    ),
                    name="notify_leboncoin_parse_recovered",
                )
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

            last_poll_instant = _now()
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
            if episode is None:
                episode = BlockEpisode(started_at=_now())
                try:
                    await save_block_episode(episode)
                except BlockEpisodeError as exc:
                    await _suspend_on_persistence_failure(exc)
                    durable_block_reason = "persistence_failed"
                    break
            logger.warning(
                "DataDome refuse la collecte Leboncoin (#%d, %s) : pause de %d s.",
                consecutive_blocks,
                block_exc,
                cooldown,
            )
            await save_poller_status({
                "status": "cooldown",
                "consecutive_blocks": consecutive_blocks,
                "last_poll_at": _now(),
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
            await _wait_until_stop_or_delay(stop_event, _bounded_wait(cooldown, episode))
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
            await _wait_until_stop_or_delay(stop_event, _bounded_wait(backoff, episode))
            continue

        except LeboncoinParseError as exc:
            consecutive_parse_failures += 1
            logger.warning(
                "Structure de page Leboncoin inattendue (%d cycles consécutifs) : %s",
                consecutive_parse_failures,
                exc,
            )
            await save_poller_status({
                "status": "parse_failure",
                "consecutive_parse_failures": consecutive_parse_failures,
                "last_poll_at": _now(),
                "message": "Structure de page Leboncoin inattendue",
            })
            if (
                consecutive_parse_failures == COLLECTION_FAILURE_ALERT_THRESHOLD
                and not parse_failure_notified
            ):
                parse_failure_notified = True
                safe_create_task(
                    notify_admins(
                        text=(
                            "⚠️ <b>BotRadar — Structure de page Leboncoin inattendue</b>\n\n"
                            f"⚠️ {consecutive_parse_failures} cycles consécutifs sans "
                            "annonce exploitable : structure inattendue ou page de "
                            "challenge non reconnue.\n\n"
                            "<i>Aucune collecte n'est possible tant que la page n'est pas "
                            "reconnue.</i>"
                        )
                    ),
                    name="notify_leboncoin_parse_degraded",
                )
            silence_notified = await _alert_silence_once(last_success_at, silence_notified)

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            logger.error("Erreur inattendue dans la boucle Leboncoin : %s", exc)

        target_interval = float(settings.LEBONCOIN_POLL_INTERVAL_SECONDS)
        jitter = random.uniform(-0.1 * target_interval, 0.15 * target_interval)
        sleep_time = max(MIN_CYCLE_GAP_SECONDS, target_interval + jitter)
        work_seconds = _monotonic() - cycle_started
        sleep_time = remaining_cycle_sleep(sleep_time, work_seconds)

        await _wait_until_stop_or_delay(stop_event, _bounded_wait(sleep_time, episode))

    if durable_block_reason is None:
        await save_poller_status({
            "status": "stopped",
            "last_poll_at": _now(),
            "message": "Collecte Leboncoin arrêtée",
        })
        logger.info("Poller Leboncoin arrêté proprement.")
