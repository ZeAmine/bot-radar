"""Poller Vinted local haute fréquence relayant les annonces vers le serveur BotRadar."""

import asyncio
import contextlib
import logging
import os
import random
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Charger les variables d'environnement depuis le fichier .env
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

# Ajouter le répertoire racine au PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.vinted_scraper import (
    DEFAULT_GLOBAL_BD_URL,
    DEFAULT_VINTED_HEADERS,
    fetch_vinted_page,
    to_ingest_payload,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("radar.local_poller")

RADAR_API_URL = os.environ.get(
    "BOTRADAR_API_URL", os.environ.get("RADAR_API_URL", "https://radar.aminezegmou.fr")
).rstrip("/")
INGEST_API_TOKEN = os.environ.get("INGEST_API_TOKEN", "your_secure_ingest_token")
INTERNAL_API_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "your_secure_internal_token")
POLL_INTERVAL_SECONDS = int(os.environ.get("VINTED_POLL_INTERVAL_SECONDS", "20"))


async def sync_cookies_to_vps(client: httpx.AsyncClient) -> bool:
    """Récupère des cookies frais depuis l'IP résidentielle et les injecte dans le serveur Radar."""
    try:
        resp = await client.get("https://www.vinted.fr/", headers=DEFAULT_VINTED_HEADERS, timeout=10.0)
        if resp.status_code == 200:
            cookies = dict(client.cookies)
            endpoint = f"{RADAR_API_URL}/api/v1/internal/vinted/session"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
            }
            res = await client.post(endpoint, headers=headers, json=cookies, timeout=10.0)
            if res.status_code == 200:
                logger.info("🍪 Session Vinted résidentielle synchronisée avec succès sur le serveur !")
                return True
    except Exception as exc:
        logger.debug("Erreur lors de la synchronisation de session: %s", exc)
    return False


async def fetch_active_custom_searches(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Récupère les recherches personnalisées actives (Tech, Gaming, Vêtements) depuis l'API Radar."""
    try:
        endpoint = f"{RADAR_API_URL}/api/v1/internal/alerts"
        headers = {"Authorization": f"Bearer {INTERNAL_API_TOKEN}"}
        resp = await client.get(endpoint, headers=headers, timeout=10.0)
        if resp.status_code == 200:
            alerts = resp.json()
            searches: list[tuple[str, str]] = []
            for a in alerts:
                url = a.get("vinted_search_url")
                cat = a.get("category") or "tech"
                if url and cat != "bd_comics":
                    searches.append((url, cat))
            return searches
    except Exception as exc:
        logger.debug("Impossible de charger les recherches personnalisées : %s", exc)
    return []


async def run_local_poller() -> None:
    """Boucle principale du poller local relayant vers l'API de production."""
    logger.info("=" * 65)
    logger.info("🚀 Démarrage du Vinted Poller Local (Relais Résidentiel Haute Performance)")
    logger.info("📡 Destination : %s", RADAR_API_URL)
    logger.info("⏱️  Fréquence    : %ds (+/- jitter)", POLL_INTERVAL_SECONDS)
    logger.info("=" * 65)

    seen_ids: set[str] = set()
    endpoint = f"{RADAR_API_URL}/api/v1/ingest/listings"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {INGEST_API_TOKEN}",
    }

    last_cookie_sync: float = 0
    last_searches_refresh: float = 0
    custom_searches: list[tuple[str, str]] = []
    poll_cycle = 0

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        # Phase 1: Premier cycle pour initialiser les IDs déjà présents
        logger.info("Initialisation du cache des annonces récentes...")
        initial_items = await fetch_vinted_page(DEFAULT_GLOBAL_BD_URL, client)
        for item in initial_items:
            seen_ids.add(item.external_id)
        logger.info("Cache initialisé avec %d annonces Vinted actives.", len(seen_ids))

        # Synchronisation initiale des cookies vers le serveur
        await sync_cookies_to_vps(client)
        last_cookie_sync = time.time()

        # Phase 2: Boucle infinie de surveillance en direct
        logger.info("Surveillance Vinted en temps réel active. En attente de nouvelles annonces...")
        while True:
            poll_cycle += 1
            now = time.time()

            # Renouveler les cookies sur le serveur toutes les 20 minutes
            if now - last_cookie_sync > 1200:
                await sync_cookies_to_vps(client)
                last_cookie_sync = now

            # Rafraîchir les recherches personnalisées toutes les 5 minutes
            if now - last_searches_refresh > 300:
                custom_searches = await fetch_active_custom_searches(client)
                last_searches_refresh = now

            try:
                # 1. Surveiller le flux Livres global
                items = await fetch_vinted_page(DEFAULT_GLOBAL_BD_URL, client)
                if items:
                    new_items = [it for it in items if it.external_id not in seen_ids]
                    if new_items:
                        logger.info("⚡ %d nouvelle(s) annonce(s) Livres détectée(s) !", len(new_items))
                        for item in new_items:
                            seen_ids.add(item.external_id)
                            payload = to_ingest_payload(item, category_slug="bd_comics")
                            try:
                                resp = await client.post(endpoint, headers=headers, json=payload, timeout=12.0)
                                if resp.status_code == 200:
                                    data = resp.json()
                                    matches = data.get("matches", [])
                                    if matches:
                                        logger.info(
                                            "🎯 MATCH LIVRE CONFIRMÉ ! '%s' (%.2f €) -> %d alerte(s) envoyée(s) !",
                                            item.title,
                                            item.item_price_cents / 100,
                                            len(matches),
                                        )
                            except Exception as exc:
                                logger.debug("Erreur envoi annonce: %s", exc)

                # 2. Surveiller 1 recherche personnalisée (Tech / Raspberry Pi, etc.) en round-robin
                if custom_searches:
                    custom_url, custom_slug = custom_searches[poll_cycle % len(custom_searches)]
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    c_items = await fetch_vinted_page(custom_url, client)
                    if c_items:
                        new_c_items = [it for it in c_items if it.external_id not in seen_ids]
                        if new_c_items:
                            logger.info(
                                "⚡ %d nouvelle(s) annonce(s) détectée(s) sur recherche %s (%s) !",
                                len(new_c_items),
                                custom_url[:50],
                                custom_slug,
                            )
                            for item in new_c_items:
                                seen_ids.add(item.external_id)
                                payload = to_ingest_payload(item, category_slug=custom_slug)
                                try:
                                    resp = await client.post(endpoint, headers=headers, json=payload, timeout=12.0)
                                    if resp.status_code == 200:
                                        data = resp.json()
                                        matches = data.get("matches", [])
                                        if matches:
                                            logger.info(
                                                "🎯 MATCH %s CONFIRMÉ ! '%s' (%.2f €) -> %d alerte(s) envoyée(s) !",
                                                custom_slug.upper(),
                                                item.title,
                                                item.item_price_cents / 100,
                                                len(matches),
                                            )
                                except Exception as exc:
                                    logger.debug("Erreur envoi annonce: %s", exc)

                # Gestion de la mémoire : éviter la croissance infinie du set
                if len(seen_ids) > 15000:
                    seen_ids = set(list(seen_ids)[-8000:])

            except Exception as exc:
                logger.error("Erreur cycle poller : %s", exc)

            # Pause intelligente avec léger jitter
            jitter = random.uniform(-1.5, 2.0)
            await asyncio.sleep(max(5.0, POLL_INTERVAL_SECONDS + jitter))


def main() -> None:
    try:
        asyncio.run(run_local_poller())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Arrêt manuel du poller local.")


if __name__ == "__main__":
    main()
