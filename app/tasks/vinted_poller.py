"""Tâche d'arrière-plan de surveillance et d'ingestion automatique du flux Vinted."""

import asyncio
import contextlib
from datetime import datetime, timedelta
import json
import logging
import random
import time
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import redis.asyncio as aioredis
from sqlalchemy import select

from app.api.dependencies import get_redis_pool
from app.core.config import settings
from app.core.constants import AlertStatus, CategorySlug
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

logger = logging.getLogger("radar.vinted_poller")

VINTED_POLLER_STATUS_KEY = "radar:vinted_poller_status"


async def save_poller_status(data: dict[str, Any]) -> None:
    """Enregistre l'état actuel du poller dans Redis (TTL 24h)."""
    try:
        pool = get_redis_pool()
        client = aioredis.Redis(connection_pool=pool)
        await client.set(VINTED_POLLER_STATUS_KEY, json.dumps(data), ex=86400)
    except Exception as exc:
        logger.debug("Impossible d'enregistrer l'état du poller dans Redis: %s", exc)


async def get_poller_status() -> dict[str, Any] | None:
    """Récupère l'état actuel du poller depuis Redis."""
    try:
        pool = get_redis_pool()
        client = aioredis.Redis(connection_pool=pool)
        raw = await client.get(VINTED_POLLER_STATUS_KEY)
        if raw:
            return json.loads(raw)
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


async def load_initial_seen_ids() -> set[str]:
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
            return set(result.scalars().all())
    except Exception as exc:
        logger.warning("Impossible de charger les anciens IDs d'annonces : %s", exc)
        return set()


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


async def poll_vinted_once(
    seen_ids: set[str],
    vinted_client: httpx.AsyncClient,
    api_client: httpx.AsyncClient,
    api_url: str,
    api_token: str,
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
    ingested_count = 0

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_token}",
    }
    endpoint = f"{api_url.rstrip('/')}/api/v1/ingest/listings"

    for item in new_items:
        seen_ids.add(item.external_id)
        payload = to_ingest_payload(item, category_slug=category_slug)

        try:
            res = await api_client.post(endpoint, headers=headers, json=payload, timeout=12.0)
            if res.status_code == 200:
                data = res.json()
                matches = data.get("matches", [])
                if matches:
                    logger.info(
                        "🎯 MATCH VINTED ! Titre: '%s' (Prix: %.2f €) -> %d alertes notifiées !",
                        item.title,
                        item.item_price_cents / 100,
                        len(matches),
                    )
                ingested_count += 1
            else:
                logger.warning("Échec ingestion HTTP %s: %s", res.status_code, res.text[:200])
        except Exception as exc:
            logger.debug(
                "Erreur lors de l'envoi de l'annonce %s vers l'API interne : %s",
                item.external_id,
                exc,
            )

    return ingested_count


async def run_vinted_poller(stop_event: asyncio.Event) -> None:
    """Boucle principale de surveillance Vinted haute performance."""
    if not settings.VINTED_POLLER_ENABLED:
        logger.info("Vinted poller désactivé dans la configuration.")
        return

    logger.info(
        "Démarrage du Vinted Poller haute fréquence (intervalle: %ds, API: %s)...",
        settings.VINTED_POLL_INTERVAL_SECONDS,
        settings.RADAR_API_URL,
    )

    seen_ids = await load_initial_seen_ids()
    logger.info("Vinted poller initialisé avec %d IDs en mémoire.", len(seen_ids))

    api_token = settings.INGEST_API_TOKEN.get_secret_value()
    api_url = settings.RADAR_API_URL
    proxy_url = settings.get_vinted_proxy()
    if proxy_url:
        logger.info("Vinted poller configuré avec proxy actif.")

    async with (
        httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            proxy=proxy_url,
        ) as vinted_client,
        httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
        ) as api_client,
    ):
        consecutive_blocks = 0
        poll_cycle = 0

        while not stop_event.is_set():
            # 0. Vérification du Mode Nuit (suspension nocturne pour préserver l'IP et éviter les bans)
            if is_night_time():
                remaining = get_night_mode_remaining_seconds()
                hours_left = int(remaining // 3600)
                mins_left = int((remaining % 3600) // 60)
                logger.info(
                    "🌙 MODE NUIT ACTIF (%02d:00 - %02d:00 %s) : Suspension de la surveillance Vinted "
                    "pendant encore %dh%02dm pour préserver l'IP et respecter le calme nocturne.",
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
                    "message": f"Mode Nuit actif (veille jusqu'à {settings.NIGHT_MODE_END_HOUR:02d}:00)",
                })
                vinted_session_manager.clear_session()
                # Pause cadencée par tranches de 60s max pour rester immédiatement réactif à stop_event
                while is_night_time() and not stop_event.is_set():
                    sleep_chunk = min(60.0, max(5.0, get_night_mode_remaining_seconds()))
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop_event.wait(), timeout=sleep_chunk)

                if stop_event.is_set():
                    break

                logger.info("☀️ FIN DU MODE NUIT : Reprise de la surveillance Vinted haute performance.")
                consecutive_blocks = 0
                await asyncio.sleep(random.uniform(2.0, 5.0))
                continue

            poll_cycle += 1
            try:
                # 1. Surveiller le flux global Livres (capture 100% des nouveautés en continu)
                count_bd = await poll_vinted_once(
                    seen_ids=seen_ids,
                    vinted_client=vinted_client,
                    api_client=api_client,
                    api_url=api_url,
                    api_token=api_token,
                    target_url=DEFAULT_GLOBAL_BD_URL,
                    category_slug=CategorySlug.BD_COMICS.value,
                )

                count_custom = 0
                # 2. Surveiller les recherches actives hors-livres (Électronique, Vêtements, Jeux Vidéo)
                custom_searches = await load_active_custom_searches()
                if custom_searches and not stop_event.is_set():
                    # Interroger 1 recherche personnalisée par cycle en round-robin pour préserver l'IP
                    custom_url, custom_slug = custom_searches[poll_cycle % len(custom_searches)]
                    await asyncio.sleep(random.uniform(1.2, 2.5))
                    count_custom = await poll_vinted_once(
                        seen_ids=seen_ids,
                        vinted_client=vinted_client,
                        api_client=api_client,
                        api_url=api_url,
                        api_token=api_token,
                        target_url=custom_url,
                        category_slug=custom_slug,
                    )

                # Si une alerte de blocage avait eu lieu et qu'on réussit, réinitialiser et avertir
                if consecutive_blocks > 0:
                    logger.info("🛡️ Garde anti-blocage : Connexion Vinted rétablie avec succès (IP propre).")
                    consecutive_blocks = 0
                    asyncio.create_task(
                        notify_admins(
                            text=(
                                "✅ <b>Radar Bot — Surveillance Vinted rétablie</b>\n\n"
                                "🟢 La connexion à Vinted est de nouveau opérationnelle !\n"
                                "Le scan des nouvelles annonces a repris normalement."
                            )
                        )
                    )

                await save_poller_status({
                    "status": "active",
                    "consecutive_blocks": 0,
                    "cooldown_until": None,
                    "cooldown_seconds": 0,
                    "last_poll_at": time.time(),
                    "last_items_found": count_bd + count_custom,
                    "message": "Surveillance Vinted active",
                })

                # Si trop d'IDs en mémoire, purger les plus anciens
                if len(seen_ids) > 15000:
                    seen_ids = set(list(seen_ids)[-8000:])

            except (VintedBlockError, VintedRateLimitError) as block_exc:
                consecutive_blocks += 1
                # Progression adaptée pour Cloudflare : 3m (180s), 7m (420s), 15m (900s), 30m (1800s), max 60m (3600s)
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

                vinted_session_manager.clear_session()

                # Notification Telegram proactive
                asyncio.create_task(
                    notify_admins(
                        text=(
                            f"🛡️ <b>Alerte Radar Bot — Pause de sécurité</b>\n\n"
                            f"⚠️ <b>{reason_str}</b> détecté sur Vinted.\n"
                            f"Mise en pause de protection de l'IP : <b>Palier #{consecutive_blocks} ({cooldown_mins} min)</b>.\n\n"
                            f"⏱ <b>Reprise prévue à</b> : {resume_time_str}\n"
                            f"<i>Le bot tentera automatiquement de se reconnecter à l'heure indiquée.</i>"
                        )
                    )
                )

                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=cooldown)
                continue

            except Exception as exc:
                logger.error("Erreur inattendue dans la boucle du Vinted Poller: %s", exc)

            # Jitter naturel humain (+/- 4s)
            jitter = random.uniform(-4.0, 5.0)
            sleep_time = max(8.0, settings.VINTED_POLL_INTERVAL_SECONDS + jitter)

            # Micro-pause naturelle humaine (30-45s) tous les ~50 cycles pour briser la régularité d'un robot
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
