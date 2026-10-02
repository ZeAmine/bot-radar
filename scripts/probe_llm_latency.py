#!/usr/bin/env python3
"""Mesure la latence réelle des appels IA sur les prompts de production.

Ce script existe parce qu'un réglage ne devrait pas être décidé sur une hypothèse.
Les variantes et exclusions d'une alerte sont vides dès que l'appel dépasse
`AI_TIMEOUT_SECONDS` : l'erreur est absorbée, le récap omet les lignes, et rien ne
le dit. Avant de corriger, on mesure.

Quatre mesures, sur les vrais prompts du dépôt :

1. `parsing`            le parsing `/add`, référence de latence ;
2. `variantes_actuel`   le prompt variantes avec le payload d'aujourd'hui ;
3. `variantes_sans_raisonnement`  idem, mode raisonnement désactivé ;
4. `variantes_longue`   idem, sans raisonnement, avec 30 s de budget.

Usage :
    python scripts/probe_llm_latency.py
    python scripts/probe_llm_latency.py --title "Regards" --category bd_comics

La clé API n'est jamais affichée. Un secret qui n'est pas configuré n'est pas une
erreur : le script le dit et s'arrête.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.http_client import get_shared_http_client  # noqa: E402
from app.services.ai.modules.keyword_variants import (  # noqa: E402
    OPTIMIZE_SYSTEM_PROMPT,
    KeywordVariantsModule,
)
from app.services.ai.modules.natural_add import SYSTEM_PROMPT as PARSE_SYSTEM_PROMPT  # noqa: E402

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)


def _describe(raw: str) -> str:
    """Dit si la réponse est exploitable, sans la réciter en entier."""
    text = (raw or "").strip()
    if not text:
        return "reponse vide"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        head = text[:80].replace("\n", " ")
        return f"pas du JSON ({head}…)"
    if isinstance(parsed, dict):
        return f"JSON valide, clés : {', '.join(sorted(parsed))}"
    return f"JSON valide ({type(parsed).__name__})"


async def _call(
    label: str,
    payload: dict[str, object],
    timeout: float,
) -> tuple[str, float | None]:
    """Un POST chronométré. Rend le verdict et la latence, jamais la clé."""
    url = f"{settings.AI_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.AI_API_KEY.get_secret_value()}",
        "Content-Type": "application/json",
    }
    print(f"\n{BOLD}{label}{RESET}  {DIM}timeout {timeout}s{RESET}")
    started = time.monotonic()
    try:
        response = await get_shared_http_client().post(
            url, json=payload, headers=headers, timeout=timeout
        )
    except httpx.TimeoutException:
        elapsed = time.monotonic() - started
        print(f"  {RED}EXPIRE{RESET} en {elapsed:.1f}s")
        return "expiré", elapsed
    except httpx.HTTPError as exc:
        elapsed = time.monotonic() - started
        print(f"  {RED}ERREUR RESEAU{RESET} {type(exc).__name__} : {str(exc)[:80]}")
        return "erreur réseau", elapsed

    elapsed = time.monotonic() - started
    if response.status_code >= 400:
        print(f"  {RED}HTTP {response.status_code}{RESET} en {elapsed:.1f}s")
        print(f"  {DIM}{response.text[:200]}{RESET}")
        return f"HTTP {response.status_code}", elapsed

    try:
        content = response.json()["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        content = ""

    verdict = _describe(content)
    colour = GREEN if verdict.startswith("JSON valide") else YELLOW
    print(f"  {colour}{verdict}{RESET}  {DIM}en {elapsed:.1f}s{RESET}")
    if content:
        print(f"  {DIM}début : {content[:160]}{RESET}")
    return verdict, elapsed


async def run(title: str, category: str) -> int:
    print(f"{BOLD}Mesure des appels IA{RESET}")
    print(f"  modèle  : {settings.AI_MODEL}")
    print(f"  base    : {settings.AI_BASE_URL}")
    print(f"  timeout configuré : {settings.AI_TIMEOUT_SECONDS}s")
    print(f"  cible   : {title!r} ({category})")

    # Les prompts sont repris tels que les modules les construisent : mesurer autre
    # chose ne prouverait rien sur la latence réelle.
    system = OPTIMIZE_SYSTEM_PROMPT
    user = KeywordVariantsModule._build_user_prompt(title, category, None)

    def payload(*, thinking: bool, temperature: float | None) -> dict[str, object]:
        body: dict[str, object] = {
            "model": settings.AI_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        if temperature is not None:
            body["temperature"] = temperature
        if not thinking:
            body["thinking"] = {"type": "disabled"}
        return body

    results: dict[str, tuple[str, float | None]] = {}

    results["parsing"] = await _call(
        "1. parsing `/add` (référence)",
        {
            "model": settings.AI_MODEL,
            "messages": [
                {"role": "system", "content": PARSE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Requête utilisateur : {title}, max 10€",
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        },
        settings.AI_TIMEOUT_SECONDS,
    )

    results["variantes_actuel"] = await _call(
        "2. variantes — payload d'aujourd'hui (raisonnement actif par défaut)",
        payload(thinking=True, temperature=0.3),
        settings.AI_TIMEOUT_SECONDS,
    )

    results["variantes_sans_raisonnement"] = await _call(
        "3. variantes — mode raisonnement désactivé",
        payload(thinking=False, temperature=None),
        settings.AI_TIMEOUT_SECONDS,
    )

    results["variantes_longue"] = await _call(
        "4. variantes — sans raisonnement, budget 30 s",
        payload(thinking=False, temperature=None),
        30.0,
    )

    results["parsing_sans_raisonnement"] = await _call(
        "5. parsing `/add` — sans raisonnement",
        {
            "model": settings.AI_MODEL,
            "messages": [
                {"role": "system", "content": PARSE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Requête utilisateur : {title}, max 10€",
                },
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0.1,
        },
        settings.AI_TIMEOUT_SECONDS,
    )

    print(f"\n{BOLD}Verdict{RESET}")
    budget = settings.AI_TIMEOUT_SECONDS
    for name, (verdict, elapsed) in results.items():
        latence = f"{elapsed:.1f}s" if elapsed is not None else "-"
        fits = "tient dans le budget" if (elapsed or 0) <= budget else "HORS BUDGET"
        print(f"  {name:32s} {latence:>7s}  {verdict[:34]:36s} {DIM}{fits}{RESET}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="Regards")
    parser.add_argument("--category", default="bd_comics")
    args = parser.parse_args()

    if not settings.AI_API_KEY.get_secret_value():
        print(f"{RED}AI_API_KEY absente.{RESET} Configurez .env puis relancez.")
        return 2
    if not settings.AI_ENABLED:
        print(f"{RED}AI_ENABLED=false.{RESET} Rien à mesurer.")
        return 2
    if not KeywordVariantsModule().is_enabled:
        print(
            f"{YELLOW}Le module variantes est désactivé "
            f"(AI_FEATURE_KEYWORD_VARIANTS) : la mesure reste valable, "
            f"le module ne le serait pas.{RESET}"
        )
    return asyncio.run(run(args.title, args.category))


if __name__ == "__main__":
    sys.exit(main())
