#!/usr/bin/env python3
"""Confronte les alertes porteuses d'un ISBN à la notice que l'autorité lui attribue.

Un ISBN faux n'est pas anodin : il contraint le matching, l'URL de recherche Vinted
et la clé de cote marché. Une alerte qui en porte un décrit donc un ouvrage, et cet
ouvrage a une notice. Ce script lit cette notice et signale ce qui ne colle plus :
un ISBN rattaché à un autre ouvrage, un ISBN que personne ne connaît, un titre, un
auteur ou un éditeur qui divergent, une variante qui cite le code d'un autre livre.

Chaque verdict nomme l'autorité qui a réellement répondu. La BNF d'abord ; quand elle
ne connaît pas l'ISBN, OpenLibrary tranche en repli — et ce verdict n'est pas une
confirmation : il est compté à part et ne se lit jamais comme une alerte conforme.

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

Chaque rapport commence par son en-tête : l'instant où la population a été lue et
l'effectif réellement audité, inactives comprises. Sans lui, deux exécutions de la même
commande — le bot créant des alertes entre les deux — seraient incomparables.

Le `--json` porte le même en-tête : il rend un objet (`genere_le`, `population`,
`alertes`) et non un tableau nu, faute de quoi sa population resterait sans nom.

Codes de sortie : 0 rien à signaler, 1 anomalies, 2 erreur. Un verdict de repli n'est
pas une anomalie : il sort en 0 et se lit `REPLI` dans le rapport.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.constants import AlertStatus
from app.core.http_client import close_shared_http_client, get_shared_http_client
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.session import AsyncSessionLocal
from app.domain.rules.listing_match import carries_foreign_isbn
from app.services.book_metadata_service import (
    AUTORITE_BNF,
    AUTORITE_OPENLIBRARY,
    SAME_WORK_TITLE_SIMILARITY_MIN,
    TITLE_SIMILARITY_MIN,
    BookMetadataService,
    IsbnVerdict,
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

# Seuls les libellés sont ici : les identifiants viennent du service, et la liste des
# autorités réellement interrogées vient du verdict, qui l'écrit en faisant les appels.
LIBELLE_AUTORITE = {AUTORITE_BNF: "la BNF", AUTORITE_OPENLIBRARY: "OpenLibrary"}

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
    # Le rapport doit dire combien d'alertes en pause il a regardées : sans cela,
    # deux rapports portant le même nombre d'anomalies ne se comparent pas.
    inactive: bool = False


@dataclass(frozen=True)
class Anomaly:
    """Une divergence nommée. `detail` dit ce que l'autorité a répondu."""

    code: str
    detail: str


def format_instant(audit_le: datetime) -> str:
    """Un instant lisible et sans ambiguïté de fuseau."""
    return audit_le.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def libelle_autorite(source: str | None) -> str:
    """Nom l'autorité qui a répondu, tel que la fiche le déclare.

    Une source inconnue est rendue telle quelle plutôt que devinée : « la BNF » par
    défaut réaffirmerait une consultation qui n'a peut-être pas eu lieu, c'est-à-dire
    exactement l'accusation que ce rapport existe pour éviter.
    """
    if source is None:
        return "aucune autorité"
    return LIBELLE_AUTORITE.get(source, source)


def inspect_identity(
    identity: AlertIdentity,
    verdict: IsbnVerdict,
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

    Chaque message nomme l'autorité qui a répondu : « la BNF » ne doit pas se lire sur
    une notice OpenLibrary, et « personne ne connaît cet ISBN » ne s'écrit que si le
    verdict porte les autorités réellement consultées — liste vide comprise.
    """
    findings: list[Anomaly] = []
    notice = verdict.notice if verdict.a_tranche else None
    autorite = libelle_autorite(verdict.autorite)

    if notice is None:
        # Le verdict dit qui a été consulté — et un code mal formé n'a été soumis à
        # personne : accuser une autorité de l'ignorer reviendrait d'inventer la
        # consultation qui a produit ce verdict.
        if verdict.consulted:
            consultees = " ni ".join(libelle_autorite(a) for a in verdict.consulted)
            findings.append(
                Anomaly(ISBN_INCONNU, f"ni {consultees} ne connaissent l'ISBN {identity.isbn}")
            )
        else:
            findings.append(
                Anomaly(
                    ISBN_INCONNU,
                    f"ISBN mal formé ({identity.isbn}) : aucune autorité interrogée",
                )
            )
    else:
        similarity = title_similarity(notice.title, identity.name)
        if similarity < TITLE_SIMILARITY_MIN:
            findings.append(
                Anomaly(ISBN_CONFLIT, f"{autorite} rattache cet ISBN à « {notice.title} »")
            )
        elif similarity < SAME_WORK_TITLE_SIMILARITY_MIN:
            findings.append(
                Anomaly(TITRE_DIVERGENT, f"{autorite} intitule l'ouvrage « {notice.title} »")
            )

        if identity.author and notice.author and not same_author(identity.author, notice.author):
            findings.append(
                Anomaly(AUTEUR_DIVERGENT, f"{autorite} donne l'auteur « {notice.author} »")
            )
        if identity.publisher and notice.publisher and (
            normalize_text(identity.publisher) != normalize_text(notice.publisher)
        ):
            findings.append(
                Anomaly(EDITEUR_DIVERGENT, f"{autorite} donne l'éditeur « {notice.publisher} »")
            )

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
    """Ce que l'audit a conclu pour une alerte, et ce que l'autorité lui a répondu.

    `verdict` est celui du service : il dit qui a été consulté, qui a tranché, et si
    personne n'a pu répondre. L'audit n'y ajoute que ses divergences, calculées sur
    cette même notice. Écrire « aucune autorité n'a répondu » à côté d'une autorité
    nommée redevient impossible — c'était ce que trois champs facultatifs permettaient,
    et le rapport affichait les deux à la fois.
    """

    identity: AlertIdentity
    verdict: IsbnVerdict
    anomalies: list[Anomaly] = field(default_factory=list)

    @property
    def confirmee_par_la_bnf(self) -> bool:
        """Une alerte n'est conforme que si la BNF elle-même a répondu."""
        return self.verdict.autorite == AUTORITE_BNF

    @property
    def motif(self) -> str | None:
        """Pourquoi l'alerte a été mise de côté ; `None` dès que quelqu'un a répondu."""
        if self.verdict.panne is None:
            return None
        motif = f"autorité muette : {self.verdict.panne}"
        if self.verdict.notice is not None:
            # Une notice de repli obtenue après un silence de la BNF ne l'a pas
            # remplacée, elle s'y ajoute : le dire empêche de lire une panne comme
            # une confirmation, et comme un refus.
            motif += " — notice de repli écartée : la BNF n'a pas tranché"
        return motif


async def audit(
    identities: Sequence[AlertIdentity],
    lookup: Callable[[str], Awaitable[IsbnVerdict]],
    *,
    delay: float = BNF_PAUSE_SECONDS,
    pause: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[AlertVerdict]:
    """Interroge l'autorité notice par notice et applique la règle à chaque verdict.

    Deux issues, une seule exigence : ni une panne ni une requête isolée qui échoue
    ne peuvent devenir une accusation. Le `silence` du verdict couvre la seconde —
    la BNF répond à la sonde globale mais pas à cette notice — et l'exception couvre
    une panne remontée jusqu'ici. Dans les deux cas l'alerte est mise de côté, et le
    rapport le dit. C'est la seule chose qui rend un audit crédible.
    """
    verdicts: list[AlertVerdict] = []
    asked = False
    for identity in identities:
        if asked:
            await pause(delay)
        asked = True
        try:
            reponse = await lookup(identity.isbn)
        except Exception as exc:  # noqa: BLE001 — une panne réseau n'est pas un verdict
            reponse = IsbnVerdict(outcome="silence", panne=f"{type(exc).__name__}: {exc}")
        if reponse.outcome == "silence":
            # Personne n'a pu répondre, repli ou non : l'alerte est mise de côté, et
            # le motif le dit. Une indisponibilité ne doit jamais se lire comme un refus.
            verdicts.append(AlertVerdict(identity, verdict=reponse))
            continue
        verdicts.append(
            AlertVerdict(identity, verdict=reponse, anomalies=inspect_identity(identity, reponse))
        )
    return verdicts


def population(verdicts: Sequence[AlertVerdict]) -> tuple[int, int]:
    """Effectif audité, inactives comprises — une seule fois, pour les deux rendus.

    Le texte et le `--json` doivent nommer la même population : la calculer à deux
    endroits les laisserait diverger au premier oubli.
    """
    return len(verdicts), sum(1 for v in verdicts if v.identity.inactive)


def render_entete(verdicts: Sequence[AlertVerdict], audit_le: datetime) -> str:
    """L'instant et la population : sans eux, un chiffre n'est opposable à personne.

    Le bot crée des alertes pendant que l'audit tourne. Deux exécutions de la même
    commande ont donc des dénominateurs différents à juste titre : sans cette ligne,
    « 24 anomalies » ne dit rien de ce qu'elles décrivaient, ni de quand.
    """
    auditees, inactives = population(verdicts)
    return (
        f"Audit d'identité — {format_instant(audit_le)}\n"
        f"Population auditée : {auditees} alerte(s) porteuse(s) d'un ISBN "
        f"(dont {inactives} inactive(s))"
    )


def render(
    verdicts: Sequence[AlertVerdict],
    *,
    quiet: bool = False,
    audit_le: datetime,
) -> str:
    """Rapport lisible : en-tête daté, une ligne par anomalie, un seul verdict à la fin.

    L'en-tête de l'alerte n'est écrit qu'à la première de ses anomalies : le répéter
    devant chacune ferait croire à autant d'alertes que de divergences.

    Une alerte que seule OpenLibrary a pu vérifier ne reçoit jamais le marqueur `ok` :
    elle est comptée à part. « Conforme » signifie « chaque alerte confirmée par la
    BNF » : un verdict sans autorité ne vaut pas davantage qu'un repli, même s'il
    n'arrive pas par le chemin d'`audit`.
    """
    lines: list[str] = [render_entete(verdicts, audit_le), ""]
    anomalies = unverified = replis = 0
    # Une réponse venue d'ailleurs que la BNF, et elle seule. Un ISBN que personne ne
    # connaît n'est pas un repli : c'est une absence de réponse, déjà signalée comme
    # anomalie. Compter les deux ensemble dirait « réponse de : aucune autorité ».
    replis_autorites: set[str] = set()

    for verdict in verdicts:
        head = f"{DIM}{verdict.identity.alert_id[:8]}{RESET} {verdict.identity.name}"
        motif = verdict.motif
        if motif:
            unverified += 1
            lines.append(f"  {YELLOW}?{RESET}      {head} {DIM}— {motif}{RESET}")
            continue

        confirmee = verdict.confirmee_par_la_bnf
        if verdict.verdict.a_tranche and not confirmee:
            replis += 1
            replis_autorites.add(libelle_autorite(verdict.verdict.autorite))

        if not verdict.anomalies:
            if confirmee:
                if not quiet:
                    lines.append(
                        f"  {GREEN}ok{RESET}      {head} {DIM}({verdict.identity.isbn}){RESET}"
                    )
            else:
                # Le repli reste visible même en mode discret : un `ok` ici ferait
                # passer une absence de confirmation pour une confirmation.
                lines.append(
                    f"  {YELLOW}REPLI{RESET}    {head} {DIM}({verdict.identity.isbn}) "
                    f"— {libelle_autorite(verdict.verdict.autorite)} a répondu, pas la BNF{RESET}"
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
    if replis:
        reponses = ", ".join(sorted(replis_autorites))
        lines.append(
            f"{YELLOW}{replis} alerte(s) non confirmée(s) par la BNF — "
            f"réponse de : {reponses}.{RESET}"
        )
    if unverified:
        lines.append(
            f"{YELLOW}{unverified} alerte(s) non vérifiées "
            f"(réseau ou autorité indisponible).{RESET}"
        )
    # `repls` ne suffit pas à characterizes : un verdict sans autorité n'y est pas
    # compté, et il n'est pas davantage une confirmation.
    if not anomalies and not unverified and all(v.confirmee_par_la_bnf for v in verdicts):
        lines.append(f"{GREEN}{len(verdicts)} alerte(s) conforme(s) aux notices.{RESET}")
    return "\n".join(lines)


def as_json(verdicts: Sequence[AlertVerdict], *, audit_le: datetime) -> str:
    """La même information sans mise en forme : lisible par une machine.

    L'objet porte l'instant et la population au même endroit que le texte : un JSON
    sans en-tête décrirait une population que rien ne nomme.
    """
    auditees, inactives = population(verdicts)
    return json.dumps(
        {
            "genere_le": audit_le.astimezone(UTC).isoformat(),
            "population": {"auditees": auditees, "inactives": inactives},
            "alertes": [
                {
                    "alert_id": v.identity.alert_id,
                    "name": v.identity.name,
                    "isbn": v.identity.isbn,
                    "anomalies": [asdict(a) for a in v.anomalies],
                    "unverified_reason": v.motif,
                    "autorite": v.verdict.autorite,
                }
                for v in verdicts
            ],
        },
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
            inactive=alert.status != AlertStatus.ACTIVE,
        )
        for alert, author, publisher in rows
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Confronte les alertes porteuses d'un ISBN à la notice de l'autorité qui "
            "le connaît, en nommant cette autorité dans chaque verdict."
        )
    )
    parser.add_argument("--json", action="store_true", help="sortie JSON")
    parser.add_argument("--limit", type=int, help="nombre maximal d'alertes auditées")
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="inclure les alertes en pause ou supprimées",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="n'afficher que les anomalies et les verdicts de repli",
    )
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

    # L'instant où la population a été lue : c'est elle que le rapport décrit, et non
    # l'heure de fin, qui dépend de la lenteur des autorités. Prise ici, il vaut aussi
    # pour une population vide — un rapport muet doit rester daté, lui aussi.
    population_lue_le = datetime.now(UTC)

    if not identities:
        if args.json:
            print(as_json([], audit_le=population_lue_le))
        else:
            print(render_entete([], population_lue_le))
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
        verdicts = await audit(identities, service.consult_authorities)
    finally:
        await close_shared_http_client()

    if args.json:
        print(as_json(verdicts, audit_le=population_lue_le))
    else:
        print(render(verdicts, quiet=args.quiet, audit_le=population_lue_le))
    return EXIT_ANOMALIES if any(v.anomalies for v in verdicts) else EXIT_CLEAN


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
