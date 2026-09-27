"""Script utilitaire pour synchroniser et injecter des cookies de session Vinted vers BotRadar."""

import argparse
import json
import logging
import os
import sys

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar.sync_session")

DEFAULT_VINTED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def fetch_fresh_cookies() -> dict[str, str]:
    """Récupère des cookies et jetons d'authentification Vinted frais."""
    with httpx.Client(timeout=10.0, follow_redirects=True, headers=DEFAULT_VINTED_HEADERS) as client:
        resp = client.get("https://www.vinted.fr/")
        if resp.status_code != 200:
            logger.error("Échec de la récupération des cookies Vinted (statut: %s)", resp.status_code)
            return {}
        cookies = dict(client.cookies)
        logger.info("Récupération réussie de %d cookies Vinted (anon_id: %s)", len(cookies), bool(cookies.get("anon_id")))
        return cookies


def push_session_to_radar(
    api_url: str,
    internal_token: str,
    cookies: dict[str, str],
) -> bool:
    """Pousse le dictionnaire de cookies vers l'endpoint interne de BotRadar."""
    endpoint = f"{api_url.rstrip('/')}/api/v1/internal/vinted/session"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {internal_token}",
    }
    with httpx.Client(timeout=10.0) as client:
        try:
            resp = client.post(endpoint, headers=headers, json=cookies)
            if resp.status_code == 200:
                logger.info("Session Vinted injectée avec succès dans BotRadar: %s", resp.json())
                return True
            logger.error("Échec de l'injection (HTTP %s): %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.error("Erreur de connexion à l'API BotRadar: %s", exc)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronise une session Vinted vers BotRadar.")
    parser.add_argument(
        "--api-url",
        default=os.environ.get(
            "BOTRADAR_API_URL", os.environ.get("RADAR_API_URL", "https://radar.aminezegmou.fr")
        ),
        help="URL de base de l'API BotRadar",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("INTERNAL_API_TOKEN", ""),
        help="Token d'authentification interne (INTERNAL_API_TOKEN)",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Affiche uniquement les cookies récupérés en JSON",
    )
    args = parser.parse_args()

    cookies = fetch_fresh_cookies()
    if not cookies:
        sys.exit(1)

    if args.print_only:
        print(json.dumps(cookies, indent=2))
        return

    if not args.token:
        logger.error("Erreur: INTERNAL_API_TOKEN requis (via --token ou variable d'environnement).")
        sys.exit(1)

    success = push_session_to_radar(args.api_url, args.token, cookies)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
