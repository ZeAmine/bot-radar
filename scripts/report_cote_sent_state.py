#!/usr/bin/env python3
"""Mesure avec quelle cote les alertes BD sont réellement parties.

Le diagnostic au repos ne peut pas trancher la question : le job glissant finit par
coter un livre lié, si bien que la base affiche « cote utilisable » longtemps après
une alerte partie sans ligne. Ce que l'utilisateur a vu, lui, n'existe qu'à
l'instant de l'envoi.

`record_sent_cote_state` incrémente donc un compteur par état, **après** que
l'alerte est partie (invariant n°1 : rien ne retarde l'alerte). Ce script lit ces
compteurs et rapporte la répartition :

- `real` : une cote chiffrée a été affichée ;
- `pending` : un livre était identifié, sa cote en cours de calcul ;
- `no_offer` : un livre était identifié, une passe a eu lieu et n'a trouvé aucune
offre — l'attente n'est plus la bonne lecture, le résultat est connu ;
- `searching` : aucun livre résolu, la recherche de l'ouvrage a été annoncée ;
- `uncatalogued` : aucun livre résolu, et la recherche a **conclu** qu'aucune
source ne catalogue l'ouvrage — l'attente n'était plus de mise (ADR-041).

Les compteurs ne mesurent que depuis leur mise en service : un rapport lu juste
après un déploiement ne décrit pas l'historique, il décrit ce qui suit. Un total
nul se lit « rien mesuré encore », jamais « aucune anomalie ».

Lecture seule : le script n'écrit rien, pas même un compteur.

Usage :
    python scripts/report_cote_sent_state.py
    python scripts/report_cote_sent_state.py --json

En production :
    docker compose exec app python scripts/report_cote_sent_state.py

Codes de sortie : 0 rapport produit, 2 lecture impossible.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from app.core.redis import close_redis
from app.services.market_health import (
    COTE_STATE_KEY_PREFIX,
    COTE_STATES,
    read_sent_cote_states,
)

EXIT_OK, EXIT_ERROR = 0, 2

GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"

# Libellés humains : le code de l'état reste stable dans le `--json`.
LIBELLE = {
    "real": "cote chiffrée affichée",
    "pending": "livre identifié, cote en cours",
    "no_offer": "livre identifié, aucune offre trouvée",
    "searching": "aucun livre résolu, recherche annoncée",
    "uncatalogued": "aucun livre résolu, aucune source ne le catalogue",
    "unavailable": "livre identifié, collecte échouée sans cote",
}


def _percent(part: int, whole: int) -> str:
    if whole <= 0:
        return "—"
    return f"{part / whole * 100:.1f} %".replace(".", ",")


def render(counts: dict[str, int], *, genere_le: datetime) -> str:
    """Rapport lisible : en-tête daté, une ligne par état, la part de muettes à la fin."""
    total = sum(counts.values())
    lines = [
        f"Répartition des cotes à l'envoi — "
        f"{genere_le.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Alertes BD mesurées depuis la mise en service des compteurs : {total}",
        "",
    ]
    for state in COTE_STATES:
        count = counts.get(state, 0)
        lines.append(
            f"  {count:>6}  {_percent(count, total):>7}  {LIBELLE[state]} "
            f"{DIM}({state}){RESET}"
        )
    if total == 0:
        lines.append("")
        lines.append(
            f"{YELLOW}Rien n'est encore mesuré : les compteurs sont incrémentés "
            f"depuis l'envoi des nouvelles alertes.{RESET}"
        )
    else:
        without_real = total - counts.get("real", 0)
        lines.append("")
        lines.append(
            f"{GREEN}{counts.get('real', 0)}/{total} alerte(s) sont parties avec une "
            f"cote chiffrée.{RESET} {without_real} sans prix immédiat."
        )
    return "\n".join(lines)


def as_json(counts: dict[str, int], *, genere_le: datetime) -> str:
    total = sum(counts.values())
    return json.dumps(
        {
            "genere_le": genere_le.astimezone(UTC).isoformat(),
            "cle_redis": f"{COTE_STATE_KEY_PREFIX}<etat>",
            "total": total,
            "etats": {state: counts.get(state, 0) for state in COTE_STATES},
        },
        ensure_ascii=False,
        indent=2,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Répartition, à l'envoi, des cotes des alertes BD."
    )
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    return parser


async def run(args: argparse.Namespace) -> int:
    genere_le = datetime.now(UTC)
    try:
        counts = await read_sent_cote_states()
    except Exception as exc:
        print(f"Lecture des compteurs impossible : {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        await close_redis()

    if args.json:
        print(as_json(counts, genere_le=genere_le))
    else:
        print(render(counts, genere_le=genere_le))
    return EXIT_OK


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
