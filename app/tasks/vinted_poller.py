"""Tâche d'arrière-plan de surveillance et d'ingestion automatique du flux Vinted."""

import asyncio
import contextlib
import json
import logging
import random
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.constants import AlertStatus, CategorySlug
from app.core.redis import get_redis_client
from app.core.tasks import safe_create_task
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.listing import Listing
from app.db.session import AsyncSessionLocal
from app.services.telegram_service import notify_admins
from app.services.vinted_scraper import (
    DEFAULT_GLOBAL_BD_URL,
    VintedBlockError,
    VintedRateLimitError,
    fetch_vinted_page,
    to_ingest_payload,
    vinted_session_manager,
)
from app.utils.percentile import interpolated_percentile

logger = logging.getLogger("radar.vinted_poller")

VINTED_POLLER_STATUS_KEY = "radar:vinted_poller_status"

# Nombre d'écarts entre cycles conservés pour la métrique de cadence. Assez pour
# absorber une micro-pause ou un cycle lent sans que le p50 soit tiré par un seul
# incident, assez court pour que la cadence affichée suive un changement réel.
CYCLE_GAP_SAMPLES = 40

# Au-delà, l'écart n'est plus un cycle de scrutation mais une interruption : reprise
# du mode nuit, fin d'une pause anti-blocage de 3 à 60 min, ou blocage du process.
# L'inclure ferait lire 30 min de p50 là où la cadence réelle est de 15 s.
MAX_MEANINGFUL_CYCLE_GAP_S = 90.0

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

    # Heures de pointe Vinted : Déjeuner (11h30 - 14h00) et Soirée (18h00 - 22h30)
    lunch_peak = 11.5 <= time_float <= 14.0
    evening_peak = 18.0 <= time_float <= 22.5
    return lunch_peak or evening_peak


def get_adaptive_poll_interval() -> float:
    """Calcule l'intervalle cible de scrutation selon le contexte réseau et horaire.

    - Avec proxy résidentiel (Raspberry Pi à domicile) : haute cadence (8s en pointe, 10s standard).
    - Sur IP VPS datacenter : cadence équilibrée (12s en pointe, 15s standard).
    """
    base = float(settings.VINTED_POLL_INTERVAL_SECONDS)
    has_residential_proxy = bool(settings.get_vinted_proxy())

    if has_residential_proxy:
        return min(base, 8.0 if is_peak_hours() else 10.0)

    if is_peak_hours():
        return min(base, 12.0)
    return max(base, 15.0)


async def load_initial_seen_ids() -> FifoSet:
    """Charge les IDs d'annonces récemment enregistrées en base pour éviter les doublons."""
    try:
        async with AsyncSessionLocal() as session:
            stmt = (
                select(Listing.external_id)
                .where(Listing.source_name == "vinted")
                .order_by(Listing.received_at.desc())
                .limit(3000)
            )
            result = await session.execute(stmt)
            raw_ids = list(result.scalars().all())
            return FifoSet(reversed(raw_ids))
    except Exception as exc:
        logger.warning("Impossible de charger les anciens IDs d'annonces : %s", exc)
        return FifoSet()


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
            return [(url, cat_slug) for url, cat_slug in result.all() if url]
    except Exception as exc:
        logger.warning("Impossible de charger les recherches personnalisées : %s", exc)
        return []


# Les recherches personnalisées ne changent que sur action de l'utilisateur
# (création, modification ou suppression d'une alerte). Les relire à chaque cycle,
# soit toutes les 12 à 15 secondes, est une requête SQL sans objet.
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

    payload_dict = to_ingest_payload(item, category_slug=category_slug)
    payload = ListingIngestPayload.model_validate(payload_dict)
    async with _get_ingest_semaphore(), AsyncSessionLocal() as session:
        use_case = ProcessListingUseCase(session)
        response = await use_case.execute(payload, raw_payload=payload_dict)
        notified_count = sum(1 for match in response.matches if match.notification_sent)
        return ScrapedIngestResult(
            notified_count=notified_count,
            needs_retry=response.needs_retry,
        )


async def poll_vinted_once(
    seen_ids: set[str],
    vinted_client: httpx.AsyncClient,
    target_url: str = DEFAULT_GLOBAL_BD_URL,
    category_slug: str = "bd_comics",
) -> int:
    """Exécute un cycle de scraping sur une URL cible et pousse les nouvelles annonces.

    Retourne le nombre de nouvelles annonces ingérées avec succès.
    """
    items = await fetch_vinted_page(target_url, vinted_client)
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
    if not settings.VINTED_POLLER_ENABLED:
        logger.info("Vinted poller désactivé dans la configuration.")
        return

    logger.info(
        "Démarrage du Vinted Poller haute fréquence (intervalle: %ds, ingest in-process)...",
        settings.VINTED_POLL_INTERVAL_SECONDS,
    )

    seen_ids: FifoSet = await load_initial_seen_ids()
    logger.info("Vinted poller initialisé avec %d IDs en mémoire.", len(seen_ids))

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
        # Historique des écarts entre deux cycles, pour rendre la cadence observable.
        # C'est le délai qui décide de la position dans la file : le traitement interne
        # ne pèse que ~17 ms face à des cycles de plusieurs secondes. Sans mesure, une
        # cadence dégradée se lit comme une cadence normale.
        cadence = CycleCadence()

        while not stop_event.is_set():
            # 0. Vérification du Mode Nuit (suspension nocturne pour préserver l'IP et éviter les
            # bans)
            if is_night_time():
                remaining = get_night_mode_remaining_seconds()
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
                    "message": (
                        "Mode Nuit actif (veille jusqu'à "
                        f"{settings.NIGHT_MODE_END_HOUR:02d}:00)"
                    ),
                })
                await vinted_session_manager.clear_session(vinted_client)
                # Pause cadencée par tranches de 60s max pour rester immédiatement réactif à
                # stop_event
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
                await asyncio.sleep(random.uniform(2.0, 5.0))
                continue

            poll_cycle += 1
            try:
                # 1. Surveiller le flux global Livres (capture 100% des nouveautés en continu)
                count_bd = await poll_vinted_once(
                    seen_ids=seen_ids,
                    vinted_client=vinted_client,
                    target_url=DEFAULT_GLOBAL_BD_URL,
                    category_slug=CategorySlug.BD_COMICS.value,
                )

                count_custom = 0
                # 2. Surveiller les recherches actives hors-livres (Électronique, Vêtements, Jeux
                # Vidéo)
                custom_searches = await get_active_custom_searches_cached()
                if custom_searches and not stop_event.is_set():
                    # Interroger 1 recherche personnalisée par cycle en round-robin pour préserver
                    # l'IP
                    custom_url, custom_slug = custom_searches[poll_cycle % len(custom_searches)]
                    await asyncio.sleep(random.uniform(1.2, 2.0))
                    count_custom = await poll_vinted_once(
                        seen_ids=seen_ids,
                        vinted_client=vinted_client,
                        target_url=custom_url,
                        category_slug=custom_slug,
                    )

                # Si une alerte de blocage avait eu lieu et qu'on réussit, réinitialiser et avertir
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
                    "message": "Surveillance Vinted active",
                })

                # Si trop d'IDs en mémoire, purger les plus anciens
                if len(seen_ids) > seen_ids.max_size:
                    seen_ids.trim()

            except (VintedBlockError, VintedRateLimitError) as block_exc:
                consecutive_blocks += 1
                # Progression adaptée pour Cloudflare : 3m (180s), 7m (420s), 15m (900s), 30m
                # (1800s), max 60m (3600s)
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
                    "Blocage Cloudflare (403)"
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
                    "reason": reason_str,
                    "resume_time": resume_time_str,
                    "message": f"Pause de sécurité #{consecutive_blocks} ({cooldown_mins} min)",
                })

                await vinted_session_manager.clear_session(vinted_client)

                # Notification Telegram proactive
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
                continue

            except Exception as exc:
                logger.error("Erreur inattendue dans la boucle du Vinted Poller: %s", exc)

            # Cadence adaptative (heures de pointe vs standard, VPS vs proxy résidentiel)
            target_interval = get_adaptive_poll_interval()
            jitter = random.uniform(-2.5, 3.5)
            sleep_time = max(6.0, target_interval + jitter)

            # Micro-pause naturelle humaine (30-45s) tous les ~50 cycles pour briser la régularité
            # d'un robot
            if poll_cycle % random.randint(45, 65) == 0:
                micro_pause = random.uniform(30.0, 45.0)
                logger.debug("🛡️ Micro-pause naturelle de %.1fs...", micro_pause)
                sleep_time += micro_pause

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_time)

        await save_poller_status({
            "status": "stopped",
            "last_poll_at": time.time(),
            "message": "Surveillance Vinted arrêtée",
        })

    logger.info("Vinted Poller arrêté proprement.")
