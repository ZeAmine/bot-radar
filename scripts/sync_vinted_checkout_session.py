"""Injecte une session Vinted authentifiée pour préparer les checkouts (bouton « Acheter »).

Cette session est **celle du compte** : elle sert à créer, à la demande, la
transaction et le panier d'une annonce (voir `docs/vinted-checkout.md`). Le
paiement, le choix du point relais et sa confirmation restent manuels, dans le
navigateur ou l'application Vinted de l'utilisateur.

Capture (une fois, puis à nouveau quand la session expire) :

1. se connecter à https://www.vinted.fr dans son navigateur ;
2. ouvrir les outils de développement → onglet Réseau → n'importe quelle
   requête vers `www.vinted.fr` ou `api.vinted.fr` ;
3. copier la valeur de l'en-tête de requête `cookie` (elle inclut les cookies
   HttpOnly, contrairement à `document.cookie`) ;
4. la coller dans un fichier hors dépôt, par exemple
   `~/.vinted-checkout-cookies` (`chmod 600`), ou la passer à `--cookie-header`.

Le script **valide** la session auprès de Vinted (`GET /api/v2/users/current`)
avant de l'injecter, et n'affiche jamais la valeur d'un cookie ni d'un jeton.
Le mot de passe n'est jamais demandé ni stocké.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.providers.vinted_checkout import (  # noqa: E402
    AUTH_COOKIE_NAMES,
    web_request_headers,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar.sync_checkout_session")

VALIDATION_URL = "https://www.vinted.fr/api/v2/users/current"
DEFAULT_COOKIE_FILE = "~/.vinted-checkout-cookies"


def parse_cookie_input(raw: str) -> dict[str, str]:
    """Lit un en-tête `cookie` recopié ou un objet JSON, sans jamais loguer les valeurs."""
    text = raw.strip()
    if not text:
        return {}
    if text[0] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            logger.error("Fichier JSON illisible.")
            return {}
        if not isinstance(data, dict):
            logger.error("Le JSON doit être un objet nom → valeur.")
            return {}
        return {
            str(name): str(value).strip()
            for name, value in data.items()
            if isinstance(name, str) and str(value).strip()
        }

    # En-tête recopié : `cookie: a=b; c=d` ou directement `a=b; c=d`.
    if ":" in text.split(";", 1)[0] and "=" not in text.split(":", 1)[0]:
        text = text.split(":", 1)[1]
    cookies: dict[str, str] = {}
    for part in text.replace("\n", " ").split(";"):
        name, _, value = part.strip().partition("=")
        if name and value:
            cookies[name] = value
    return cookies


def validate_session(cookies: dict[str, str]) -> tuple[bool, str]:
    """Confirme que la session est bien celle d'un compte connecté.

    Un `200` **ne suffit pas** : la passerelle Vinted rend un corps JSON porteur
    d'un `code` (`0` = succès, autre = refus, ex. `106` « Accès refusé » /
    `user_login_required`). Le verdict se lit donc sur le corps, comme le fait
    le client web ; le statut HTTP ne sert qu'à distinguer une panne réseau.
    """
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            response = client.get(VALIDATION_URL, headers=web_request_headers(cookies))
    except httpx.HTTPError as exc:
        return False, f"Vinted injoignable ({type(exc).__name__})."

    body_code: int | None = None
    message_code = ""
    try:
        data = response.json()
        if isinstance(data, dict):
            if isinstance(data.get("code"), int):
                body_code = data["code"]
            if isinstance(data.get("message_code"), str):
                message_code = data["message_code"]
    except ValueError:
        message_code = "corps_illisible"

    if response.status_code != 200:
        code = f" code={body_code}" if body_code is not None else ""
        msg = f" message_code={message_code}" if message_code else ""
        return False, f"Vinted refuse la session (HTTP {response.status_code}{code}{msg})."

    if body_code is None or body_code != 0:
        code = f" code={body_code}" if body_code is not None else " code=absent"
        msg = f" message_code={message_code}" if message_code else ""
        return False, f"Vinted rend un succès sans identité confirmée ({code}{msg})."

    return True, "session reconnue par Vinted (code=0)."


def push_session_to_radar(api_url: str, internal_token: str, cookies: dict[str, str]) -> bool:
    """Envoie la session validée à l'endpoint interne — valeurs jamais réaffichées."""
    endpoint = f"{api_url.rstrip('/')}/api/v1/internal/vinted/checkout-session"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {internal_token}",
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(endpoint, headers=headers, json=cookies)
    except httpx.HTTPError as exc:
        logger.error("Erreur de connexion à l'API BotRadar : %s", type(exc).__name__)
        return False
    if response.status_code == 200:
        data = response.json()
        logger.info(
            "Session injectée : %s cookies (%s).",
            data.get("cookies_stored", len(cookies)),
            ", ".join(sorted(cookies)),
        )
        return True
    logger.error("Échec de l'injection (HTTP %s) : %s", response.status_code, response.text[:160])
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Injecte une session Vinted de compte pour le bouton « Acheter »."
    )
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
        "--cookie-file",
        default=DEFAULT_COOKIE_FILE,
        help="Fichier hors dépôt contenant l'en-tête cookie recopié (ou un JSON).",
    )
    parser.add_argument(
        "--cookie-header",
        default="",
        help="En-tête cookie donné directement (prioritaire sur --cookie-file).",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Valide la session auprès de Vinted sans l'injecter.",
    )
    args = parser.parse_args()

    raw = args.cookie_header
    if not raw:
        path = Path(args.cookie_file).expanduser()
        if not path.is_file():
            logger.error(
                "Fichier de cookies introuvable : %s (voir docs/vinted-checkout.md).", path
            )
            sys.exit(2)
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            logger.warning(
                "Le fichier de cookies %s est lisible au-delà de son propriétaire "
                "(mode %o) : resserre-le avec `chmod 600`.",
                path,
                mode,
            )
        raw = path.read_text(encoding="utf-8")

    cookies = parse_cookie_input(raw)
    if not cookies:
        logger.error("Aucun cookie lisible dans l'entrée fournie.")
        sys.exit(2)
    if not any(cookies.get(name) for name in AUTH_COOKIE_NAMES):
        logger.error(
            "Cookies d'identité absents : la session ne vient pas d'un compte connecté."
        )
        sys.exit(2)
    logger.info("Cookies lus : %s.", ", ".join(sorted(cookies)))

    valid, reason = validate_session(cookies)
    if not valid:
        logger.error("Session invalide : %s", reason)
        sys.exit(2)
    logger.info("Session valide : %s", reason)

    if args.validate_only:
        return

    if not args.token:
        logger.error("Erreur: INTERNAL_API_TOKEN requis (via --token ou variable d'environnement).")
        sys.exit(2)
    if not push_session_to_radar(args.api_url, args.token, cookies):
        sys.exit(1)


if __name__ == "__main__":
    main()
