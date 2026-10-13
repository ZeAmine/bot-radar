#!/usr/bin/env python3
"""Découvre si l'API catalogue Vinted expose un horodatage de publication.

Ce script existe parce qu'une décision de mesure ne devrait pas reposer sur une
hypothèse. La colonne `listings.published_at` existe depuis la migration initiale
et le champ d'ingestion aussi, mais **aucun producteur ne les remplit** : le chemin
du poller ne transporte aucune date. Or `parse_vinted_catalog_json` ne lit aucun
champ de date, et aucun fixture de réponse réelle du dépôt ne prouve qu'un tel
champ existe côté Vinted. Avant d'écrire la moindre propagation, on regarde.

Portée : **une seule requête catalogue**, `per_page=1`. Le gestionnaire de session
peut y ajouter au plus un rafraîchissement, déjà bridé à 15 s. Aucune boucle,
aucun retry, aucune pagination, aucun appel par annonce. L'exécution s'arrête au
premier statut non 200 ou à la première exception, et n'appelle jamais
`clear_session` — elle ne prend aucun risque sur la session de production.

Usage :
    python scripts/probe_vinted_published_at.py

**Résultat mesuré le 2026-10-13** (une requête) : la réponse du catalogue porte les clés
`content_source, favourite_count, id, is_favourite, item_box, photo, photos, price,
promoted, search_tracking_params, service_fee, show_1st_time_seller_discount, title,
total_item_price, url, user, view_count` — **aucun horodatage de publication**. Le seul
nom retenu par la détection est `show_1st_time_seller_discount`, un booléen : le « time »
de « first time », pas une date. La propagation et la mesure de publication ont été
closes en « bloqué » sur ce constat. Le script reste pour reproduire ou reverifier la
chose, pas pour être cru sur parole.

Le script n'affiche que des **clés** et des valeurs courtes. Aucun cookie, jeton,
`Authorization` ni `X-Anon-Id` n'est imprimé. À exécuter à la main, en local, hors
CI et hors `pytest`.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services.vinted_parsing import catalog_url_to_api_params  # noqa: E402
from app.services.vinted_scraper import (  # noqa: E402
    API_CATALOG_ENDPOINT,
    DEFAULT_GLOBAL_BD_URL,
    vinted_session_manager,
)

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)

# Sous-objets dont les clés méritent d'être listées : ce sont eux qu'un
# horodatage de publication logerait, pas nécessairement au premier niveau.
_NESTED_OBJECTS = ("item_box", "price", "photo", "user", "size")
_NESTED_LISTS = ("photos",)

# Un nom de clé qui évoque une date. Volontairement large : la sonde doit lever
# des candidats à qualifier à la main, pas décider à leur place. Un `updated_at`
# peut être une republication, un bump ou un rafraîchissement — pas la date de
# publication d'origine. Seule la relecture de la valeur départage.
_DATE_HINTS = (
    "date", "time", "created", "updated", "published", "upload", "bump",
    "refresh", "timestamp",
)
_DATE_SUFFIXES = ("_at", "_ts", "_time", "_date")
_DATE_EXACT = frozenset({"ts", "at", "date"})


def is_date_like(key: object) -> bool:
    """Dit si un nom de clé évoque un horodatage."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if lowered in _DATE_EXACT:
        return True
    return any(hint in lowered for hint in _DATE_HINTS) or lowered.endswith(_DATE_SUFFIXES)


def describe_item(item: object) -> tuple[list[str], dict[str, list[str]]]:
    """Rend les clés de premier niveau et celles des sous-objets connus d'un item."""
    if not isinstance(item, dict):
        return [], {}
    top = sorted(str(key) for key in item)
    nested: dict[str, list[str]] = {}
    for name in _NESTED_OBJECTS:
        value = item.get(name)
        if isinstance(value, dict):
            nested[name] = sorted(str(key) for key in value)
    for name in _NESTED_LISTS:
        value = item.get(name)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            nested[f"{name}[0]"] = sorted(str(key) for key in value[0])
    return top, nested


def dig(payload: object, key: str) -> object:
    """Cherche une clé dans tout l'arbre et rend sa valeur (première trouvée)."""
    if not isinstance(payload, dict):
        return None
    stack: list[dict] = [payload]
    while stack:
        current = stack.pop()
        for name, value in current.items():
            if name == key:
                return value
            if isinstance(value, dict):
                stack.append(value)
    return None


def date_like_keys(payload: object) -> list[str]:
    """Clés évoquant un horodatage, cherchées dans tout l'arbre d'un objet."""
    if not isinstance(payload, dict):
        return []
    found: set[str] = set()
    stack: list[dict] = [payload]
    while stack:
        current = stack.pop()
        for key, value in current.items():
            if is_date_like(key):
                found.add(str(key))
            if isinstance(value, dict):
                stack.append(value)
    return sorted(found)


def summarize_value(value: object, limit: int = 60) -> str:
    """Résume une valeur courte : assez pour juger, jamais un secret ni une charge."""
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}…"


def request_params() -> dict[str, str]:
    """Paramètres de la requête unique : la page demandée, réduite à un item."""
    params = catalog_url_to_api_params(DEFAULT_GLOBAL_BD_URL)
    params["per_page"] = "1"
    return params


async def _probe() -> int:
    """Une requête, aucune boucle : le script se lit et s'arrête."""
    params = request_params()
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        proxy=settings.get_vinted_proxy(),
    ) as client:
        await vinted_session_manager.ensure_valid_session(client)
        response = await client.get(
            API_CATALOG_ENDPOINT,
            params=params,
            headers=vinted_session_manager.get_api_headers(),
        )

    if response.status_code != 200:
        print(f"{RED}HTTP {response.status_code}{RESET} — aucune donnée exploitable.")
        return 1

    try:
        data = response.json()
    except ValueError:
        print(f"{RED}réponse non JSON{RESET} — aucune donnée exploitable.")
        return 1

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        print(f"{YELLOW}aucun item dans la réponse{RESET}.")
        return 1

    item = items[0]
    print(f"\n{BOLD}Item 1/{len(items)}{RESET}  {DIM}{len(response.content)} octets{RESET}")
    top, nested = describe_item(item)
    print(f"  clés de premier niveau : {', '.join(top)}")
    for name, keys in nested.items():
        print(f"  clés de {name} : {', '.join(keys)}")

    candidates = date_like_keys(item)
    print(f"\n{BOLD}Clés évoquant un horodatage{RESET} ({len(candidates)})")
    if not candidates:
        print(f"  {YELLOW}aucune{RESET}")
    for key in candidates:
        print(f"  {key} = {summarize_value(dig(item, key))}")

    if candidates:
        print(
            f"\n{YELLOW}Candidats trouvés : à qualifier à la main.{RESET} Un `updated_at` ou un "
            "`bump` peut être une republication, pas la publication d'origine. Relire la "
            "valeur retenue avant toute propagation."
        )
    else:
        print(
            f"\n{GREEN}Aucun champ de date dans cette réponse{RESET} — sur un item unique, ce "
            "n'est pas une preuve pour toute l'API, mais aucun candidat ne sort du chemin "
            "de collecte actuel."
        )
    return 0


def main() -> int:
    return asyncio.run(_probe())


if __name__ == "__main__":
    sys.exit(main())
