#!/usr/bin/env python3
"""Confronte les alertes porteuses d'un ISBN à la notice que la BNF lui attribue.

Un ISBN faux n'est pas anodin : il contraint le matching, l'URL de recherche Vinted
et la clé de cote marché. Une alerte qui en porte un décrit donc un ouvrage, et cet
ouvrage a une notice. Ce script lit cette notice et signale ce qui ne colle plus :
un ISBN rattaché à un autre ouvrage, un ISBN que personne ne connaît, un titre, un
auteur ou un éditeur qui divergent, une variante qui cite le code d'un autre livre.

Il ne corrige rien, volontairement. Les mots-clés d'une alerte ont pu être retouchés
à la main, et une correction automatique détruirait cette intention sans que personne
ne l'ait demandée. Ce script signale ; corriger reste un geste volontaire.

Lecture seule : la session n'ouvre aucune transaction et n'exécute que des SELECT.

Usage :
    python scripts/audit_alerts_identity.py                 # rapport, code 1 si anomalie
    python scripts/audit_alerts_identity.py --json          # sortie machine
    python scripts/audit_alerts_identity.py --limit 20      # vingt alertes
    python scripts/audit_alerts_identity.py --quiet         # anomalies seules
    python scripts/audit_alerts_identity.py --include-inactive

En production :
    docker compose exec app python scripts/audit_alerts_identity.py

Codes de sortie : 0 rien à signaler, 1 anomalies, 2 erreur.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field

from sqlalchemy import select

from app.core.constants import AlertStatus
from app.core.http_client import close_shared_http_client, get_shared_http_client
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.session import AsyncSessionLocal
from app.domain.rules.listing_match import carries_foreign_isbn
from app.services.book_metadata_service import (
    SAME_WORK_TITLE_SIMILARITY_MIN,
    TITLE_SIMILARITY_MIN,
    BookMetadata,
    BookMetadataService,
    same_author,
    title_similarity,
)
from app.utils.text import normalize_text

# Le SRU de la BNF est une autorité publique : on l'interroge par lots, pas à la
# demande. Un quart de seconde entre deux notices est invisible pour l'utilisateur et
# pour l'autorité elle-même.
BNF_PAUSE_SECONDS = 0.35

EXIT_CLEAN, EXIT_ANOMALIES, EXIT_ERROR = 0, 1, 2

# Codes stables : ils sortent dans le `--json` et sont vérifiés par les tests.
ISBN_CONFLIT = "isbn_conflit"
ISBN_INCONNU = "isbn_inconnu"
TITRE_DIVERGENT = "titre_divergent"
AUTEUR_DIVERGENT = "auteur_divergent"
EDITEUR_DIVERGENT = "editeur_divergent"
VARIANTE_ISBN_ETRANGER = "variante_isbn_etranger"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


@dataclass(frozen=True)
class AlertIdentity:
    """Ce qu'une alerte affirme de l'ouvrage, tel qu'enregistré.

    L'auteur et l'éditeur viennent du livre lié : ils n'existent que là. Le titre
    reste celui de l'alerte, parce que c'est celui que l'utilisateur a choisi.
    """

    alert_id: str
    name: str
    isbn: str
    author: str | None = None
    publisher: str | None = None
    variants: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class Anomaly:
    """Une divergence nommée. `detail` dit ce que l'autorité a répondu."""

    code: str
    detail: str


def inspect_identity(
    identity: AlertIdentity,
    notice: BookMetadata | None,
) -> list[Anomaly]:
    """Confronte une alerte à sa notice. Fonction pure : ni base, ni réseau.

    Deux seuils déjà définis par le projet séparent deux questions qui se ressemblent
    mais ne se confondent pas :

    - sous `TITLE_SIMILARITY_MIN`, la notice ne décrit pas l'ouvrage demandé — c'est le
      verdict d'ADR-021 : l'ISBN est rattaché à un autre livre ;
    - entre ce seuil et `SAME_WORK_TITLE_SIMILARITY_MIN`, c'est bien la même œuvre
      mais pas la même formulation : une divergence à regarder, pas une contradiction.

    Ce sont les seuils de `book_metadata_service`, pas des constantes locales :
    l'audit et la création d'alerte doivent tomber d'accord sur ce qu'est un autre
    ouvrage, sinon il signalerait en faux positif ce que `/add` a accepté.
    """
    findings: list[Anomaly] = []

    if notice is None:
        findings.append(Anomaly(ISBN_INCONNU, f"aucune notice pour l'ISBN {identity.isbn}"))
    else:
        similarity = title_similarity(notice.title, identity.name)
        if similarity < TITLE_SIMILARITY_MIN:
            findings.append(
                Anomaly(ISBN_CONFLIT, f"la BNF rattache cet ISBN à « {notice.title} »")
            )
        elif similarity < SAME_WORK_TITLE_SIMILARITY_MIN:
            findings.append(
                Anomaly(TITRE_DIVERGENT, f"la notice s'intitule « {notice.title} »")
            )

        if identity.author and notice.author and not same_author(identity.author, notice.author):
            findings.append(Anomaly(AUTEUR_DIVERGENT, f"la notice donne « {notice.author} »"))
        if identity.publisher and notice.publisher and (
            normalize_text(identity.publisher) != normalize_text(notice.publisher)
        ):
            findings.append(Anomaly(EDITEUR_DIVERGENT, f"la notice donne « {notice.publisher} »"))

    foreign = [v for v in identity.variants if carries_foreign_isbn(v, identity.isbn)]
    if foreign:
        findings.append(
            Anomaly(
                VARIANTE_ISBN_ETRANGER,
                f"variante(s) citant un autre ISBN : {', '.join(foreign)}",
            )
        )
    return findings


async def bnf_joignable(client: object) -> bool:
    """L'autorité répond-elle ? Indispensable avant de croire un « personne ne connaît ».

    `lookup_by_isbn` absorbe ses propres erreurs de transport et renvoie `None` : sans
    cette sonde, une BNF injoignable se lirait comme un ISBN que personne ne connaît,
    c'est-à-dire comme une anomalie — exactement la fausse accusation que cet audit
    existe pour éviter. L'opération `explain` ne renvoie aucun enregistrement : elle
    coûte une requête et prouve la disponibilité.
    """
    try:
        reponse = await client.get(  # type: ignore[attr-defined]
            f"{BookMetadataService.BNF_SRU_URL}?version=1.2&operation=explain",
            timeout=8.0,
        )
    except Exception:  # noqa: BLE001 — l'indisponibilité est ici le verdict
        return False
    return getattr(reponse, "status_code", 0) == 200


@dataclass
class AlertVerdict:
    """Ce que l'audit a conclu pour une alerte, ou pourquoi il n'a pas pu conclure."""

    identity: AlertIdentity
    anomalies: list[Anomaly] = field(default_factory=list)
    unverified_reason: str | None = None


async def audit(
    identities: Sequence[AlertIdentity],
    lookup: Callable[[str], Awaitable[BookMetadata | None]],
    *,
    delay: float = BNF_PAUSE_SECONDS,
    pause: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[AlertVerdict]:
    """Interroge la BNF notice par notice et applique la règle à chaque réponse.

    Une exception de transport met l'alerte de côté au lieu de la compter comme
    fausse anomalie : une BNF injoignable ne prouve pas qu'un ISBN est faux, elle
    prouve seulement qu'on n'a pas su. Le rapport distingue les deux, et c'est la
    seule chose qui rend un audit crédible.
    """
    verdicts: list[AlertVerdict] = []
    asked = False
    for identity in identities:
        if asked:
            await pause(delay)
        asked = True
        try:
            notice = await lookup(identity.isbn)
        except Exception as exc:  # noqa: BLE001 — une panne réseau n'est pas un verdict
            verdicts.append(
                AlertVerdict(identity, unverified_reason=f"{type(exc).__name__}: {exc}")
            )
            continue
        verdicts.append(AlertVerdict(identity, anomalies=inspect_identity(identity, notice)))
    return verdicts


def render(verdicts: Sequence[AlertVerdict], *, quiet: bool = False) -> str:
    """Rapport lisible : une ligne par anomalie, un seul verdict à la fin.

    L'en-tête de l'alerte n'est écrit qu'à la première de ses anomalies : le répéter
    devant chacune ferait croire à autant d'alertes que de divergences.
    """
    lines: list[str] = []
    anomalies = unverified = 0

    for verdict in verdicts:
        head = f"{DIM}{verdict.identity.alert_id[:8]}{RESET} {verdict.identity.name}"
        if verdict.unverified_reason:
            unverified += 1
            lines.append(
                f"  {YELLOW}?{RESET}      {head} {DIM}— {verdict.unverified_reason}{RESET}"
            )
            continue
        if not verdict.anomalies:
            if not quiet:
                lines.append(
                    f"  {GREEN}ok{RESET}      {head} {DIM}({verdict.identity.isbn}){RESET}"
                )
            continue
        anomalies += 1
        for index, anomaly in enumerate(verdict.anomalies):
            marker = f"{RED}ANOMALIE{RESET}" if index == 0 else " " * 12
            label = head if index == 0 else ""
            lines.append(
                f"  {marker} {label} {RED}{anomaly.code}{RESET} {DIM}— {anomaly.detail}{RESET}"
            )

    lines.append("")
    if anomalies:
        lines.append(f"{RED}{anomalies} alerte(s) à regarder.{RESET}")
    if unverified:
        lines.append(
            f"{YELLOW}{unverified} alerte(s) non vérifiées "
            f"(réseau ou autorité indisponible).{RESET}"
        )
    if not anomalies and not unverified:
        lines.append(f"{GREEN}{len(verdicts)} alerte(s) conforme(s) aux notices.{RESET}")
    return "\n".join(lines)


def as_json(verdicts: Sequence[AlertVerdict]) -> str:
    """La même information sans mise en forme : lisible par une machine."""
    return json.dumps(
        [
            {
                "alert_id": v.identity.alert_id,
                "name": v.identity.name,
                "isbn": v.identity.isbn,
                "anomalies": [asdict(a) for a in v.anomalies],
                "unverified_reason": v.unverified_reason,
            }
            for v in verdicts
        ],
        ensure_ascii=False,
        indent=2,
    )


async def read_identities(
    *,
    include_inactive: bool = False,
    limit: int | None = None,
) -> list[AlertIdentity]:
    """Alertes porteuses d'un ISBN, avec l'auteur et l'éditeur de leur livre lié.

    Seul `SELECT` : la session est fermée sans commit, donc rien ne peut être écrit
    même par accident.
    """
    stmt = (
        select(Alert, Book.author, Book.publisher)
        .outerjoin(AlertBook, AlertBook.alert_id == Alert.id)
        .outerjoin(Book, Book.id == AlertBook.book_id)
        .where(Alert.isbn.is_not(None))
        .order_by(Alert.created_at)
    )
    if not include_inactive:
        stmt = stmt.where(Alert.status == AlertStatus.ACTIVE)
    if limit is not None:
        stmt = stmt.limit(limit)

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(stmt)).all()

    return [
        AlertIdentity(
            alert_id=str(alert.id),
            name=alert.name,
            isbn=alert.isbn or "",
            author=author,
            publisher=publisher,
            variants=tuple(alert.variants or ()),
        )
        for alert, author, publisher in rows
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Confronte les alertes porteuses d'un ISBN à leur notice BNF."
    )
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    parser.add_argument("--limit", type=int, help="nombre maximal d'alertes auditées")
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="inclure les alertes en pause ou supprimées",
    )
    parser.add_argument("--quiet", action="store_true", help="n'afficher que les anomalies")
    return parser


async def run(args: argparse.Namespace) -> int:
    """Le parcours complet du CLI : lire, confronter, rapporter, EXIT_*."""
    try:
        identities = await read_identities(
            include_inactive=args.include_inactive,
            limit=args.limit,
        )
    except Exception as exc:
        print(f"{RED}Lecture des alertes impossible : {exc}{RESET}", file=sys.stderr)
        return EXIT_ERROR

    if not identities:
        print("Aucune alerte ne porte d'ISBN : rien à confronter.")
        return EXIT_CLEAN

    service = BookMetadataService()
    try:
        if not await bnf_joignable(get_shared_http_client()):
            # Rien n'a pu être vérifié : l'audit n'a pas eu lieu. Aucune alerte n'est
            # signalée, et le code 2 dit que le rapport est vide parce qu'il n'a pas
            # été produit, pas parce qu'il n'y avait rien à trouver.
            print(f"{RED}La BNF ne répond pas : aucune alerte n'a pu être vérifiée.{RESET}")
            return EXIT_ERROR
        verdicts = await audit(identities, service.lookup_by_isbn)
    finally:
        await close_shared_http_client()

    if args.json:
        print(as_json(verdicts))
    else:
        print(render(verdicts, quiet=args.quiet))
    return EXIT_ANOMALIES if any(v.anomalies for v in verdicts) else EXIT_CLEAN


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
