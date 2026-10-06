"""Rafraîchit le jeton anti-bot Chasse-aux-livres avec un vrai navigateur (Playwright).

Pourquoi ce script existe
-------------------------
``scripts/sync_chasse_session.py`` valide et injecte un jeton ``duih``, mais il
exige qu'on le fournisse d'abord à la main depuis un navigateur. Ce script
automatise justement cette capture : il ouvre chasse-aux-livres.fr dans un
Chromium piloté par Playwright, laisse le défi anti-bot se résoudre, lit le
cookie ``duih`` (ou l'attribut ``data-duih`` de ``<html>``), puis délègue au
script manuel la validation et l'injection — un seul chemin de validation, pas
deux.

Repli gracieux (approche A)
---------------------------
- Playwright absent du venv : le script l'annonce avec la commande d'installation
  et sort en 2, sans casser quoi que ce soit.
- Défi non résolu dans le délai : il renvoie à la méthode manuelle (copier le
  cookie depuis un vrai navigateur), qui reste toujours valable. La passe
  cron `scripts/refresh_chasse_cron.sh` tente alors une seconde passe en
  headful sous Xvfb avant d'abandonner.
- Un jeton capturé mais inutile est rejeté par la validation, exactement comme
  en manuel : jamais d'« installé » qui ne rendrait rien.

Le jeton n'est jamais journalisé : seule sa longueur l'est, avec sa source
(attribut ``data-duih`` ou cookie), ce qui distingue un défi passé d'un cookie
d'entrée sans valeur.

Codes de sortie (lus par le cron) :
    0 = jeton injecté (ou rien à faire avec --print-only)
    1 = jeton capturé mais refusé par la validation, ou injection échouée
    2 = capture impossible (Playwright absent, défi non résolu, token interne manquant)

Usage :
    python scripts/refresh_chasse_token.py                   # capture, valide, injecte
    python scripts/refresh_chasse_token.py --print-only      # sans injection
    python scripts/refresh_chasse_token.py --headful         # voir le défi se résoudre
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sync_chasse_session import push_token_to_radar, validate_token  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar.refresh_chasse_token")

SITE_URL = "https://www.chasse-aux-livres.fr/"

# Le défi se résout en général en quelques secondes ; au-delà, on renvoie à la
# méthode manuelle plutôt que de tourner dans le vide.
CHALLENGE_TIMEOUT_SECONDS = 45.0
POLL_INTERVAL_SECONDS = 1.0

# Deux signaux mécaniques trahissent le pilote avant même le défi : le flag
# `webdriver` et des langues absentes. On les retire à l'exécution des pages.
# On n'emprunte rien d'autre — une empreinte surfaite se détecte aussi, et le
# site ne doit rien voir d'autre qu'un navigateur français normal.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['fr-FR', 'fr', 'en-US', 'en']});
"""

# Fenêtre et fuseau d'un visiteur de France : le défi se juge aussi là-dessus.
CONTEXT_OPTIONS = {
    "viewport": {"width": 1280, "height": 800},
    "locale": "fr-FR",
    "timezone_id": "Europe/Paris",
}
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


def extract_duih(html_attr: str | None, cookies: list[dict]) -> str | None:
    """Retire le jeton de l'attribut ``data-duih`` ou du cookie ``duih``.

    L'attribut ``<html data-duih>`` est prioritaire : il est rendu une fois le
    défi passé, alors qu'un cookie ``duih`` peut exister dès l'entrée — un cookie
    présent n'est pas un défi résolu.
    """
    if html_attr and html_attr.strip():
        return html_attr.strip()
    for cookie in cookies:
        if cookie.get("name") == "duih":
            value = cookie.get("value") or ""
            if value.strip():
                return value.strip()
    return None


async def capture_token(headful: bool) -> str | None:
    """Ouvre le site, attend la résolution du défi, renvoie le jeton ou ``None``."""
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error(
            "Playwright absent. Installez-le avec :\n"
            "    pip install playwright && playwright install chromium\n"
            "Ou capturez le jeton à la main : voir scripts/sync_chasse_session.py."
        )
        return None

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=not headful, args=LAUNCH_ARGS
        )
        try:
            context = await browser.new_context(**CONTEXT_OPTIONS)
            await context.add_init_script(STEALTH_INIT_SCRIPT)
            page = await context.new_page()

            try:
                await page.goto(SITE_URL, wait_until="domcontentloaded")

                loop = asyncio.get_running_loop()
                deadline = loop.time() + CHALLENGE_TIMEOUT_SECONDS
                while loop.time() < deadline:
                    html_attr = await page.get_attribute("html", "data-duih")
                    cookies = await page.context.cookies(SITE_URL)
                    token = extract_duih(html_attr, cookies)
                    if token:
                        source = (
                            "attribut data-duih"
                            if html_attr and html_attr.strip()
                            else "cookie duih"
                        )
                        logger.info(
                            "Défi résolu, jeton capturé (%s caractères, source : %s).",
                            len(token),
                            source,
                        )
                        return token
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
            except PlaywrightError as exc:
                # Page fermée, naviguée ailleurs ou navigation refusée : on le
                # dit précisément plutôt que de raconter un délai dépassé.
                logger.error(
                    "Navigation ou défi interrompu (%s) : %s — nouvelle tentative "
                    "à la passe suivante.",
                    type(exc).__name__,
                    exc,
                )
                return None

            logger.error(
                "Défi non résolu en %.0f s. Reprenez à la main : ouvrez %s dans "
                "votre navigateur, laissez le défi se résoudre, puis copiez le "
                "cookie `duih` et lancez scripts/sync_chasse_session.py.",
                CHALLENGE_TIMEOUT_SECONDS,
                SITE_URL,
            )
            return None
        finally:
            await browser.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Affiche le navigateur (pour comprendre un défi qui ne se résout pas)",
    )
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

    duih = await capture_token(headful=args.headful)
    if not duih:
        return 2

    low_cents = await validate_token(duih)
    if low_cents is None:
        logger.error(
            "Ce jeton ne rend pas la série 1 an : le défi n'est probablement pas "
            "passé. Relancez avec --headful pour le voir, ou reprenez-le à la main."
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
