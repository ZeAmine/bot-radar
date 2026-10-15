#!/usr/bin/env python3
"""Complète les tomes manquants des séries BD suivies par leur nom.

Une série surveillée par son nom n'a pas d'ISBN : ses tomes se découvrent. Cette
découverte tourne à la **création** de l'alerte et, depuis le chemin d'ingestion,
elle est **relancée** quand une annonce ne se résout à aucun tome. Une série
créée avant ce changement, ou dont la découverte n'avait rien rendu, restait
donc avec ses tomes incomplets : le panneau « 📊 Prix du marché » n'offrait pas
un bouton par tome, et le total de série était absent.

Ce script rejoue cette découverte sur les alertes **déjà enregistrées** via
l'appel exact du chemin d'ingestion — `link_books_from_bnf(..., series=True)` —
donc la même autorité (BNF d'abord, OpenLibrary en complément), les mêmes
repositories et la même cotation. Il n'ajoute ni table, ni migration, ni
dépendance, et n'écrit **aucun** total de série : celui-ci reste posé par
`persist_market_result`, la seule source de vérité.

Les cotations partent en arrière-plan (elles ne doivent jamais retarder une
alerte). Un CLI, lui, s'arrête : sans attendre, `asyncio.run` annulerait les
tâches en vol et laisserait les tomes liés **sans cote**, donc le panneau « 📊
Prix du marché » muet et le total de série absent. La passe attend donc ses
propres cotations avant de rendre la main.

Il est **idempotent** : une seconde passe ne relie rien de neuf et le rapporte.
Elle relance en revanche les autorités, qui restent donc sollicitées.

Usage :
    python scripts/backfill_series_tomes.py --dry-run
    python scripts/backfill_series_tomes.py --dry-run --json
    python scripts/backfill_series_tomes.py
    python scripts/backfill_series_tomes.py --limit 20

En production :
    docker compose exec -T app python scripts/backfill_series_tomes.py --dry-run
    docker compose exec -T app python scripts/backfill_series_tomes.py

Codes de sortie : 0 rien à faire (ou travail appliqué), 1 des tomes manquent
en mode rapport (`--dry-run`), 2 erreur.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.constants import AlertStatus, CategorySlug
from app.core.tasks import wait_all_background_tasks
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.repositories.book_repo import BookRepository
from app.db.session import AsyncSessionLocal
from app.services.book_metadata_service import BookMetadata, BookMetadataService
from app.use_cases.link_bd_catalog import link_books_from_bnf

logger = logging.getLogger("radar.backfill_series")

# Pause entre deux alertes : la BNF et OpenLibrary sont des services publics, et
# le backfill peut lier des dizaines de séries d'affilée. Un quart de seconde est
# invisible pour l'utilisateur et pour l'autorité.
AUTHORITY_PAUSE_SECONDS = 0.35

# Attente des cotations lancées par la liaison : chacune est bornée, côté marchands,
# par le délai de collecte d'un livre (20 s, `book_market_pricing`). Les passes
# tournent en parallèle, donc cette durée borne la plus lente et non leur somme.
PRICING_DRAIN_SECONDS = 30.0

EXIT_CLEAN, EXIT_REMAINING, EXIT_ERROR = 0, 1, 2

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


@dataclass(frozen=True)
class SeriesAlert:
    """Une alerte BD sans ISBN : une série à compléter."""

    alert_id: str
    name: str


@dataclass(frozen=True)
class BackfillReport:
    """Ce qu'une passe a fait — ou ferait — pour une série.

    `candidate_isbns` porte les tomes que la passe **apporte** (mode écriture) ou
    **lierait** (mode rapport). Vide, elle est à jour.
    """

    alert: SeriesAlert
    linked_before: int
    linked_after: int
    candidate_isbns: tuple[str, ...]
    dry_run: bool
    error: str | None = None


# --------------------------------------------------------------- dépendances


async def read_eligible_alerts(*, limit: int | None = None) -> list[SeriesAlert]:
    """Alertes BD actives sans ISBN — les séries que ce backfill vise.

    Une alerte porteuse d'un ISBN n'est pas une série : son ouvrage est identifié,
    elle n'a rien à compléter. Lecture seule : la session est fermée sans commit.
    """
    stmt = (
        select(Alert.id, Alert.name)
        .join(Category, Category.id == Alert.category_id)
        .where(
            Alert.status == AlertStatus.ACTIVE,
            Alert.isbn.is_(None),
            Category.slug == CategorySlug.BD_COMICS.value,
        )
        .order_by(Alert.created_at)
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(stmt)).all()

    return [SeriesAlert(alert_id=str(alert_id), name=name) for alert_id, name in rows]


async def linked_isbns(alert: SeriesAlert) -> set[str]:
    """ISBN des tomes déjà liés à l'alerte, lu par le repository du chemin réel."""
    async with AsyncSessionLocal() as session:
        books = await BookRepository(session).get_all_books_for_alert(
            uuid.UUID(alert.alert_id)
        )
    return {book.isbn13 for book in books if book.isbn13}


async def discover_tomes(alert: SeriesAlert) -> list[BookMetadata]:
    """Tomes qu'une autorité rend pour la série — BNF d'abord, OpenLibrary en complément.

    La recherche rend un **verdict** (ADR-041) : le script ne lit que ses notices,
    et c'est le seam appelé ici qui porte la lecture — un appelant qui attendrait une
    liste nue itérerait un verdict, et le mode rapport tomberait sur un `TypeError`.
    """
    verdict = await BookMetadataService().lookup_series_notices(
        alert.name, author_hint=None
    )
    return list(verdict.notices)


async def link_series(alert: SeriesAlert) -> None:
    """Rejoue l'appel exact du chemin d'ingestion : lier et coter la série entière."""
    await link_books_from_bnf(
        alert.name,
        uuid.UUID(alert.alert_id),
        author=None,
        series=True,
    )


# ------------------------------------------------------------------ backfill


async def backfill_alert(
    alert: SeriesAlert,
    *,
    dry_run: bool,
    linked: Callable[[SeriesAlert], Awaitable[set[str]]] = linked_isbns,
    discover: Callable[[SeriesAlert], Awaitable[list[BookMetadata]]] = discover_tomes,
    link: Callable[[SeriesAlert], Awaitable[None]] = link_series,
) -> BackfillReport:
    """Complète une série, ou annonce ce qu'elle a à compléter.

    En mode écriture, les tomes sont liés par `link` (le chemin d'ingestion), et
    c'est l'écart des ISBN liés avant/après qui dit ce qui a été apporté — le
    script ne recompose aucun total. En mode rapport, la découverte est interrogée
    sans rien écrire : les tomes manquants sont ceux qu'elle rend et qui ne sont
    pas déjà liés.
    """
    try:
        before = await linked(alert)
        if dry_run:
            notices = await discover(alert)
            known = {notice.isbn13 for notice in notices if notice.isbn13}
            missing = tuple(sorted(known - before))
            return BackfillReport(alert, len(before), len(before), missing, True)

        await link(alert)
        after = await linked(alert)
        return BackfillReport(
            alert, len(before), len(after), tuple(sorted(after - before)), False
        )
    except Exception as exc:  # une alerte en échec ne prive pas les suivantes
        logger.warning("Backfill de la série %s impossible : %s", alert.name, exc)
        return BackfillReport(alert, 0, 0, (), dry_run, error=f"{type(exc).__name__}: {exc}")


async def backfill(
    alerts: Sequence[SeriesAlert],
    *,
    dry_run: bool,
    linked: Callable[[SeriesAlert], Awaitable[set[str]]] = linked_isbns,
    discover: Callable[[SeriesAlert], Awaitable[list[BookMetadata]]] = discover_tomes,
    link: Callable[[SeriesAlert], Awaitable[None]] = link_series,
    pause: Callable[[float], Awaitable[None]] = asyncio.sleep,
    pause_seconds: float = AUTHORITY_PAUSE_SECONDS,
) -> list[BackfillReport]:
    """Complète chaque série, à raison d'une pause entre deux autorités sollicitées."""
    reports: list[BackfillReport] = []
    for index, alert in enumerate(alerts):
        if index:
            await pause(pause_seconds)
        reports.append(
            await backfill_alert(
                alert, dry_run=dry_run, linked=linked, discover=discover, link=link
            )
        )
    return reports


# -------------------------------------------------------------------- rendu


def format_instant(moment: datetime) -> str:
    """Un instant lisible et sans ambiguïté de fuseau."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def render(
    reports: Sequence[BackfillReport],
    *,
    generated_at: datetime,
    dry_run: bool,
) -> str:
    """Rapport lisible : en-tête daté, une ligne par série, un seul verdict à la fin."""
    mode = "rapport (aucune écriture)" if dry_run else "écriture"
    lines: list[str] = [
        f"Complément des séries — {format_instant(generated_at)}",
        f"Mode : {mode} — {len(reports)} alerte(s) BD active(s) sans ISBN",
        "",
    ]
    to_link = linked = errors = 0

    for report in reports:
        head = f"{DIM}{report.alert.alert_id[:8]}{RESET} {report.alert.name}"
        if report.error:
            errors += 1
            lines.append(f"  {YELLOW}!{RESET}      {head} {DIM}— échec : {report.error}{RESET}")
            continue
        count = len(report.candidate_isbns)
        if dry_run:
            if count:
                to_link += 1
                lines.append(
                    f"  {YELLOW}À LIER{RESET} {head} {DIM}— {count} tome(s) : "
                    f"{', '.join(report.candidate_isbns)}{RESET}"
                )
            else:
                lines.append(
                    f"  {GREEN}ok{RESET}     {head} {DIM}({report.linked_before} tome(s)){RESET}"
                )
            continue
        if count:
            linked += count
            lines.append(
                f"  {GREEN}+{count}{RESET}     {head} {DIM}— {report.linked_before} → "
                f"{report.linked_after} tomes : {', '.join(report.candidate_isbns)}{RESET}"
            )
        else:
            lines.append(
                f"  {GREEN}ok{RESET}     {head} {DIM}({report.linked_after} tome(s)){RESET}"
            )

    lines.append("")
    if dry_run:
        if to_link:
            lines.append(f"{YELLOW}{to_link} alerte(s) ont des tomes à lier.{RESET}")
        else:
            lines.append(f"{GREEN}Toutes les séries éligibles sont complètes.{RESET}")
    else:
        lines.append(f"{GREEN}{linked} tome(s) lié(s).{RESET}")
    if errors:
        lines.append(f"{YELLOW}{errors} alerte(s) en échec.{RESET}")
    return "\n".join(lines)


def as_json(
    reports: Sequence[BackfillReport],
    *,
    generated_at: datetime,
    dry_run: bool,
) -> str:
    """La même information sans mise en forme, en-tête comprise."""
    # Chaque rapport dit son mode : une passe mêlant écriture et rapport ne verrait
    # pas ses totaux autrement.
    added = sum(len(r.candidate_isbns) for r in reports if not r.dry_run and not r.error)
    pending = sum(len(r.candidate_isbns) for r in reports if r.dry_run and not r.error)
    return json.dumps(
        {
            "genere_le": generated_at.astimezone(UTC).isoformat(),
            "mode": "rapport" if dry_run else "ecriture",
            "population": {"eligibles": len(reports)},
            "alertes": [
                {
                    "alert_id": r.alert.alert_id,
                    "name": r.alert.name,
                    "linked_before": r.linked_before,
                    "linked_after": r.linked_after,
                    "isbns": list(r.candidate_isbns),
                    "erreur": r.error,
                }
                for r in reports
            ],
            "total": {"tomes_ajoutes": added, "tomes_a_lier": pending},
        },
        ensure_ascii=False,
        indent=2,
    )


def exit_code(reports: Sequence[BackfillReport], *, dry_run: bool) -> int:
    """0 rien à faire ou travail appliqué, 1 des tomes manquent, 2 erreur.

    En mode rapport, un tome manquant est ce que le script a trouvé : il le
    signale par le code de sortie, comme le nettoyeur de mots-clés. En mode
    écriture, le travail est fait — rester en échec ferait croire à une erreur.
    """
    if any(report.error for report in reports):
        return EXIT_ERROR
    if dry_run and any(report.candidate_isbns for report in reports):
        return EXIT_REMAINING
    return EXIT_CLEAN


# ---------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Complète les tomes manquants des séries BD suivies par leur nom, "
            "en rejouant la découverte de série du chemin d'ingestion."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="rapporte les tomes à lier sans rien écrire",
    )
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    parser.add_argument("--limit", type=int, help="nombre maximal de séries traitées")
    return parser


async def run(args: argparse.Namespace) -> int:
    """Le parcours complet du CLI : lire, compléter, rapporter, EXIT_*."""
    generated_at = datetime.now(UTC)
    try:
        alerts = await read_eligible_alerts(limit=args.limit)
    except Exception as exc:
        print(f"{RED}Lecture des alertes impossible : {exc}{RESET}", file=sys.stderr)
        return EXIT_ERROR

    reports = await backfill(alerts, dry_run=args.dry_run) if alerts else []

    # Les tomes viennent d'être liés ; leurs cotations courent encore en tâche de
    # fond. Sortir maintenant les annulerait, et laisserait sans prix ce que la
    # passe vient justement de rattacher.
    await wait_all_background_tasks(timeout=PRICING_DRAIN_SECONDS)

    if args.json:
        print(as_json(reports, generated_at=generated_at, dry_run=args.dry_run))
    else:
        print(render(reports, generated_at=generated_at, dry_run=args.dry_run))
    return exit_code(reports, dry_run=args.dry_run)


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
