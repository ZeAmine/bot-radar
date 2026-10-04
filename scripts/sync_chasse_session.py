"""Capture et injecte le jeton anti-bot Chasse-aux-livres, sur le modèle des cookies Vinted.

Pourquoi ce script existe
-------------------------
La série « prix bas constaté sur 1 an » de Chasse-aux-livres vient de
``/rest/jcd``. Depuis le 2026-09-11 cet endpoint ne répond plus qu'à une session
qui a passé le défi anti-bot du site : une requête HTTP seule reçoit un ``200``
vide. Le provider n'a alors plus aucun historique à fournir, et les cotes
existantes restent figées sur un chiffre de septembre.

Comment le jeton s'obtient
-------------------------
Le site ne le délivre qu'à un vrai navigateur : ouvrir chasse-aux-livres.fr,
laisser le défi se résoudre, puis lire le cookie ``duih`` (DevTools ▸ Application ▸
Cookies, ou la console : ``document.cookie``). Le site expose aussi cette valeur
dans l'attribut ``data-duih`` de la balise ``<html>``.

Ce script ne pretend pas automatiser un défi JavaScript. Il fait ce qui est
vérifiable : il **valide** le jeton avant de l'injecter, en interrogeant le même
endpoint avec le même code que la production. Un jeton qui ne rend pas la série
1 an est refuse, parce qu'un jeton « installé » mais inutile produirait exactement
le silence que ce script doit empêcher.

Le jeton n'est jamais journalisé : seuls sa longueur et son effet sont rapportés.

Usage :
    python scripts/sync_chasse_session.py --duih "<cookie duih>"
    python scripts/sync_chasse_session.py --duih-file jeton.txt
    CHASSE_AUX_LIVRES_DUIH=... python scripts/sync_chasse_session.py
    python scripts/sync_chasse_session.py --duih "..." --print-only   # sans injection
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.providers.base import extract_best_historical_low  # noqa: E402
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar.sync_chasse_session")

# Un ouvrage dont la série 1 an est publique et stable : la validation doit pouvoir
# se faire sur un titre connu, sinon « aucune offre » ferait passer pour un refus.
PROBE_ISBN = "9782380463972"
PROBE_TITLE = "Dead Train T1"


def read_token(args: argparse.Namespace) -> str:
    """Jeton depuis l'argument, le fichier ou l'environnement, jamais un log."""
    if args.duih:
        return args.duih.strip()
    if args.duih_file:
        return Path(args.duih_file).read_text(encoding="utf-8").strip()
    return (os.environ.get("CHASSE_AUX_LIVRES_DUIH") or "").strip()


async def validate_token(token: str) -> int | None:
    """Interroge la série 1 an avec ce jeton. Renvoie le prix bas, ou ``None``.

    La validation passe par le provider lui-même : c'est le seul moyen de savoir
    que le jeton marche pour nous, avec nos paramètres et notre titre de sonde —
    pas seulement pour une requête quelconque.
    """
    previous = settings.CHASSE_AUX_LIVRES_DUIH
    settings.CHASSE_AUX_LIVRES_DUIH = token
    try:
        offers = await ChasseAuxLivresProvider().get_offers(
            isbn13=PROBE_ISBN, title=PROBE_TITLE
        )
    except Exception as exc:
        logger.error("Validation impossible (%s) : %s", type(exc).__name__, exc)
        return None
    finally:
        settings.CHASSE_AUX_LIVRES_DUIH = previous

    return extract_best_historical_low(offers)


def push_token_to_radar(api_url: str, internal_token: str, token: str) -> bool:
    """Injecte le jeton dans Redis via l'endpoint interne, comme pour Vinted."""
    endpoint = f"{api_url.rstrip('/')}/api/v1/internal/chasse/session"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {internal_token}",
    }
    try:
        resp = httpx.post(
            endpoint, headers=headers, json={"duih": token}, timeout=15.0
        )
    except Exception as exc:
        logger.error("Connexion à l'API BotRadar impossible : %s", exc)
        return False
    if resp.status_code == 200:
        logger.info("Jeton injecté : %s", resp.json())
        return True
    logger.error("Injection refusée (HTTP %s) : %s", resp.status_code, resp.text[:200])
    return False


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duih", default="", help="Valeur du cookie duih")
    parser.add_argument("--duih-file", default="", help="Fichier contenant le jeton")
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
        help="Valide et affiche le résultat sans rien injecter",
    )
    args = parser.parse_args()

    duih = read_token(args)
    if not duih:
        logger.error(
            "Aucun jeton fourni. Ouvrez chasse-aux-livres.fr dans un navigateur, "
            "laissez le défi se résoudre, puis copiez le cookie `duih` "
            "(--duih / --duih-file / CHASSE_AUX_LIVRES_DUIH)."
        )
        return 2
    logger.info("Jeton fourni (%s caractères) : validation en cours...", len(duih))

    low_cents = await validate_token(duih)
    if low_cents is None:
        logger.error(
            "Ce jeton ne rend pas la série 1 an. Il est probablement périmé ou issu "
            "d'une session qui n'a pas passé le défi. Reprenez-le depuis un navigateur "
            "sur la page du livre."
        )
        return 1
    logger.info("Jeton validé : la série 1 an répond (%s centimes).", low_cents)

    if args.print_only:
        print(f'{{"validated": true, "historical_low_1y_cents": {low_cents}}}')
        return 0

    if not args.token:
        logger.error(
            "INTERNAL_API_TOKEN requis (--token ou variable d'environnement) pour injecter."
        )
        return 2
    if not push_token_to_radar(args.api_url, args.token, duih):
        return 1

    logger.info(
        "La prochaine passe de cotation lira ce jeton et remettra les cotes à jour. "
        "L'alerte « cotes figées » se taira d'elle-même."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
