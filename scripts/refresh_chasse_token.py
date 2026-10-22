"""Rafraîchit le jeton anti-bot Chasse-aux-livres par connexion au compte (ADR-036).

Pourquoi ce script existe
-------------------------
La série « prix bas constaté sur 1 an » vient de ``/rest/jcd`` et ne répond
qu'à un jeton ``duih`` délivré par une session ayant passé le défi anti-bot
(ADR-019, ADR-020). Capturé à la main, il meurt sans préavis : trois semaines
de cotes figées en septembre 2026 (ADR-034).

Pourquoi le compte, pas un navigateur
-------------------------------------
Le défi est un reCAPTCHA v2 en images que le site sert à **toute** requête
pilotée — Chromium headless ou headful confondus (constat du 06/10 : la
capture Playwright échouait en code 2 à chaque passe, sans jamais toucher au
jeton en base). La connexion au compte reste soumise aux défis de la source.
Le parcours attendu est :
``GET /login?protect=true`` rend le formulaire (champs anti-rejeu
``_sourcePage`` et ``__fp``), ``POST /login`` soumet
email / mot de passe / protect, puis un membre connecté lit ``data-duih``
sur l'accueil. Le jeton obtenu subit ensuite la **même** validation que tout
autre (``sync_chasse_session.validate_token``) : la série 1 an doit répondre,
sinon rien n'est injecté.

Ni le jeton ni les identifiants n'atteignent le journal : seules des
longueurs et des codes HTTP y figurent. Les requêtes ``httpx`` sont réduites
au niveau WARNING — elles loguent l'URL complète en INFO, dont ``/rest/jcd``
qui porte le ``duih`` (même réglage que ``app/core/logging.py``).

Codes de sortie (lus par le cron) :
    0 = jeton injecté (ou rien à faire avec --print-only)
    1 = jeton capturé mais refusé par la validation, ou injection échouée
    2 = capture impossible (identifiants absents, formulaire incomplet,
        site injoignable)

Usage :
    CHASSE_ACCOUNT_EMAIL=… CHASSE_ACCOUNT_PASSWORD=… \
        python scripts/refresh_chasse_token.py
    python scripts/refresh_chasse_token.py --print-only
    python scripts/refresh_chasse_token.py --api-url http://127.0.0.1:8000
"""

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sync_chasse_session import push_token_to_radar, validate_token  # noqa: E402

from app.core.http_client import close_shared_http_client, get_shared_http_client  # noqa: E402
from app.core.redis import close_redis  # noqa: E402
from app.providers.cal_governor import (  # noqa: E402
    CalCollectionBudget,
    CalSuspendedError,
    GovernedClient,
    cal_governor,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
# httpx journalise chaque URL complète en INFO — dont /rest/jcd qui porte le duih.
# Le secret ne doit atteindre aucun journal : même réglage que la production.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("radar.refresh_chasse_token")

BASE_URL = "https://www.chasse-aux-livres.fr"
LOGIN_URL = f"{BASE_URL}/login?protect=true"

# Chrome normal, français : le site sert une coquille vide aux clients assimilés à
# des automates (ADR-036) ; le formulaire se lit avec ces mêmes en-têtes.
LOGIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

REQUEST_TIMEOUT_SECONDS = 30.0


def extract_login_fields(html: str) -> tuple[str, str] | None:
    """Champs anti-rejeu du formulaire de connexion, ou ``None`` s'ils manquent.

    ``_sourcePage`` et ``__fp`` sont liés à la session qui a rendu la page :
    les renvoyer vides ou d'une autre page ferait échouer la connexion côté
    serveur. Leur absence signale un formulaire changé — autant le dire que
    deviner.
    """
    source_page = re.search(r'name="_sourcePage" value="([^"]+)"', html)
    fingerprint = re.search(r'name="__fp" value="([^"]+)"', html)
    if not source_page or not fingerprint:
        return None
    return source_page.group(1), fingerprint.group(1)


def duih_attribute(html: str) -> str | None:
    """Valeur de ``<html data-duih="…">``, telle qu'un membre connecté la voit."""
    match = re.search(r'data-duih="([^"]+)"', html)
    return match.group(1) if match else None


def extract_duih(html_attr: str | None, cookies: list[dict]) -> str | None:
    """Retire le jeton de l'attribut ``data-duih`` ou du cookie ``duih``.

    L'attribut ``<html data-duih>`` est prioritaire : le site ne le rend qu'à
    une session qui a passé le défi, alors qu'un cookie ``duih`` peut traîner
    d'une entrée antérieure — un cookie présent n'est pas un jeton confirmé.
    """
    if html_attr and html_attr.strip():
        return html_attr.strip()
    for cookie in cookies:
        if cookie.get("name") == "duih":
            value = cookie.get("value") or ""
            if value.strip():
                return value.strip()
    return None


async def _login_request(
    client: GovernedClient, method: str, url: str, **kwargs,
) -> httpx.Response:
    headers = LOGIN_HEADERS
    for _ in range(client.max_redirects + 1):
        response = await getattr(client, method)(
            url, headers=headers, follow_redirects=False,
            timeout=REQUEST_TIMEOUT_SECONDS, **kwargs,
        )
        redirect = response.next_request
        if redirect is None:
            return response
        if (redirect.url.scheme, redirect.url.host, redirect.url.port) != (
            "https", "www.chasse-aux-livres.fr", None,
        ):
            raise ValueError("Redirection CAL hors origine refusée")
        method, url = redirect.method.lower(), str(redirect.url)
        headers = dict(redirect.headers)
        kwargs = {"content": await redirect.aread()} if method == "post" else {}
    raise ValueError("Trop de redirections CAL")


async def capture_token(client: GovernedClient, email: str, password: str) -> str | None:
    """Se connecte au compte, lit ``data-duih`` sur l'accueil, renvoie le jeton.

    Chaque étape logue précisément et rend ``None`` : le cron a un code à
    relire, pas d'exception à deviner. Ni mot de passe ni jeton n'atteignent
    le journal.
    """
    try:
        response = await _login_request(client, "get", LOGIN_URL)
    except httpx.HTTPError as exc:
        logger.error("Page de connexion injoignable (%s).", type(exc).__name__)
        return None
    if response.status_code != 200:
        logger.error("Page de connexion : HTTP %s.", response.status_code)
        return None

    fields = extract_login_fields(response.text)
    if fields is None:
        logger.error(
            "Formulaire de connexion incomplet (_sourcePage/__fp absents) : "
            "le formulaire a changé, le script doit le suivre."
        )
        return None
    source_page, fingerprint = fields

    try:
        response = await _login_request(
            client, "post",
            f"{BASE_URL}/login",
            data={
                "email": email,
                "password": password,
                "protect": "true",
                "_sourcePage": source_page,
                "__fp": fingerprint,
            },
        )
    except httpx.HTTPError as exc:
        logger.error("Connexion impossible (%s).", type(exc).__name__)
        return None
    if 'id="login"' in response.text:
        # Formulaire encore présent : identifiants refusés ou formulaire devenu
        # incompatible. Rien à pousser, le code de sortie 2 du cron reste le voyant.
        logger.error(
            "Connexion refusée (HTTP %s) : le formulaire est toujours présent "
            "— identifiants refusés ou formulaire changé.",
            response.status_code,
        )
        return None

    try:
        response = await _login_request(client, "get", f"{BASE_URL}/")
    except httpx.HTTPError as exc:
        logger.error("Accueil injoignable après connexion (%s).", type(exc).__name__)
        return None
    cookies = [{"name": cookie.name, "value": cookie.value} for cookie in client.cookies.jar]
    token = extract_duih(duih_attribute(response.text), cookies)
    if token:
        logger.info("Jeton lu (%s caractères).", len(token))
    else:
        logger.error("Aucun data-duih sur l'accueil d'un membre connecté.")
    return token


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Valide et affiche le résultat sans rien injecter",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get(
            "BOTRADAR_API_URL",
            os.environ.get("RADAR_API_URL", "https://radar.aminezegmou.fr"),
        ),
        help="URL de base de l'API BotRadar",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("INTERNAL_API_TOKEN", ""),
        help="Token d'authentification interne (INTERNAL_API_TOKEN)",
    )
    args = parser.parse_args()

    email = os.environ.get("CHASSE_ACCOUNT_EMAIL", "").strip()
    password = os.environ.get("CHASSE_ACCOUNT_PASSWORD", "").strip()
    if not email or not password:
        logger.error(
            "CHASSE_ACCOUNT_EMAIL / CHASSE_ACCOUNT_PASSWORD absents : "
            "connexion impossible. Exporter les deux clés ou poser le fichier "
            "account.env (chmod 600, hors dépôt) référencé par le cron."
        )
        return 2

    probe_token = None
    validated = False
    reason = "token_refresh_aborted"
    try:
        probe_token = await cal_governor.admit_book(
            "script-refresh-token", counts_as_book=False,
        )
        budget = CalCollectionBudget(cal_governor)
        budget.probe_token = probe_token
        client = GovernedClient(get_shared_http_client(), budget)
        duih = await capture_token(client, email, password)
        if not duih:
            return 2
        low_cents = await validate_token(duih, collection_budget=budget)
        validated = low_cents is not None
    except CalSuspendedError as exc:
        reason = exc.reason
        logger.warning("Renouvellement CAL reporté : %s", exc.reason)
        return 2
    except ValueError:
        logger.error("Redirection de connexion CAL refusée.")
        return 2
    finally:
        try:
            await cal_governor.conclude_probe(probe_token, succeeded=validated, reason=reason)
        finally:
            await close_shared_http_client()
            await close_redis()
    if low_cents is None:
        logger.error(
            "Connexion acceptée mais le jeton ne rend pas la série 1 an : "
            "rien n'est injecté — un jeton inutile reprendrait le silence."
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
        "Jeton injecté. La prochaine passe de cotation le lira et remettra les "
        "cotes à jour."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
