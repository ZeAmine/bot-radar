"""Admission d'un tome de série : ce qui prouve qu'il existe, et ce qui l'écarte.

`add « La Passe-miroir » de Christelle Dabos` a retenu **quatre** tomes, dont deux
qui n'ont jamais paru : la sélection des tomes savait dédoublonner et prioriser,
elle ne savait pas vérifier qu'un livre est sorti. Un ISBN ne prouve donc pas
l'existence d'un tome — la BNF publie une notice dès l'annonce.

**Ce que la zone fixe `100$a` dit, et ne dit pas.** Les positions 0-7 portent la
date d'**entrée de la notice au catalogue** : c'est le jour où la BNF a créé la
notice, pas celui où le livre est sorti. Une réimpression de 1998 cataloguée en
2026 y porte 2026. Cette date ne sert donc **jamais** de date de parution ici : le
champ s'appelle `entry_date`, et le récapitulatif ne l'affiche pas.

La parution se lit ailleurs : position 8 = **type de date**, positions 9-12 =
**Date 1** (l'année de publication déclarée). Seuls certains types autorisent à
conclure ; `ADMITTED_DATE_TYPES` les énumère et le module justifie l'ensemble.
`214$d` (« date de publication, diffusion ») est la seule zone qui puisse porter
un **jour** de parution : quand elle en porte un, il est plus précis que l'année
et il l'emporte.

Le module est pur : aucun accès réseau, aucune horloge implicite (`today` est
passé), aucun état. Il ne lève jamais et ne décide pas de l'affichage : il rend un
fait — accepté, ou refusé pour tel motif — que le récapitulatif met en phrase.

**Ce qu'il ne fait pas.** Il ne trie pas les tomes, ne choisit pas d'édition et ne
remplace pas `select_series_volumes` : celui-ci dédoublonne et priorise, celui-ci
décide si une ligne a le droit d'être proposée à l'ajout. Les deux se composent, ils
ne se dupliquent pas.

**« Au moindre doute, refus » (arbitrage commanditaire).** Un motif ne s'invente
pas : une date non prouvée s'écrit « date de parution non prouvée », jamais « tome
à paraître ». Un tome réellement à paraître le dit — il a une date certaine, dans
le futur — et cela reste une information, pas un refus silencieux.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from app.domain.rules.bibliographic_identity import author_tokens
from app.utils.isbn import is_valid_isbn, isbn_from_identifier
from app.utils.text import normalize_text

# Motifs de refus, du plus proche de la donnée au plus proche de l'œuvre.
TomeRejection = Literal[
    "invalid_isbn",
    "no_bnf_notice",
    "bnf_unavailable",
    "date_unproven",
    "not_published_yet",
    "series_mismatch",
    "tome_number_inconsistent",
    "author_mismatch",
    "publisher_mismatch",
]

# Un motif qui dit « cette notice parle d'un autre ouvrage » n'est pas une simple
# absence de preuve : la ligne décrit alors autre chose que le tome demandé, et une
# date finira par la rendre publiable. Elle quitte donc le récapitulatif, au lieu
# d'y attendre. Les autres restent : le tome existe, c'est sa preuve qui manque.
_OTHER_WORK_REASONS = frozenset({
    "invalid_isbn",
    "series_mismatch",
    "tome_number_inconsistent",
    "author_mismatch",
})

# Zone fixe `100$a` : 0-7 = entrée de la notice au catalogue, 8 = type de date,
# 9-12 = Date 1 (année de publication déclarée). Trois positions, trois sens
# différents : les confondre fut l'erreur d'origine.
_ENTRY_SLICE = slice(0, 8)
_DATE_TYPE_INDEX = 8
_YEAR_SLICE = slice(9, 13)

# Types de date de publication admis comme preuve d'une année de parution.
#
# `d` = « monographie publiée en une seule année civile » : l'année de Date 1 est
# alors celle de la parution, et rien d'autre. C'est le seul type qui affirme cela
# d'une monographie terminée.
#
# Exclus, avec leur raison :
# - `u` = inconnue / non renseignée : la notice elle-même dit qu'elle ne sait pas,
#   et c'est le cas des tomes annoncés mais non parus (« La Passe-miroir » 2 et 4) ;
# - `a`, `b`, `g` = ressources continues (publication en cours ou arrêtée) : une
#   année de Date 1 y borne un intervalle, elle ne date pas le volume ;
# - `e` = monographie à plusieurs dates (réimpression, édition composite) : la
#   Date 1 est celle de l'édition reproduite, pas celle du tirage proposé ;
# - `f`, `h`, `i`, `j` = dates détaillées, copyright ou dépôt : ni l'une ni l'autre
#   ne dit que l'exemplaire est en librairie un jour donné ;
# - `m`, `n`, `q`, `r`, `s` = encodages de dates partielles ou douteuses.
ADMITTED_DATE_TYPES = frozenset({"d"})

# Mots de maison d'édition qui ne distinguent rien : « Éditions Pocket » et
# « Pocket » sont la même maison, et un refus pour ce motif serait une erreur.
_HOUSE_NOISE = frozenset({
    "edition", "editions", "ed", "eds", "l", "le", "la", "les", "de", "du", "des",
    "d", "et", "cie", "compagnie", "groupe", "sa", "sas",
})


@dataclass(frozen=True)
class TomeNotice:
    """Ce qu'une notice BNF prouve d'un tome, et rien de plus.

    La notice vient de la BNF par construction : c'est le seul référentiel consulté
    pour une création série. Un tome sans notice est donc un tome sans preuve, et
    l'absence de notice est un motif (`no_bnf_notice`), pas un champ vide.
    """

    isbn13: str | None
    title: str = ""
    date_type: str | None = None
    entry_date: date | None = None
    declared_year: int | None = None
    publication_date: date | None = None
    declared_publication_year: int | None = None
    authors: tuple[str, ...] = ()
    series_statement: str | None = None
    number: int | None = None
    publisher: str | None = None

    @property
    def proven_day(self) -> date | None:
        """Jour de parution prouvé, ou `None`.

        Un seul champ le porte : `214$d`, et seulement quand il nomme un jour. Une
        année ne se convertit jamais en 1er janvier — ce serait réinventer le jour
        qu'on refuse d'annoncer.
        """
        return self.publication_date

    @property
    def proven_year(self) -> int | None:
        """Année de parution prouvée, ou `None`.

        Trois règles, dans cet ordre :

        1. un jour prouvé (`214$d`) donne son année — le jour contient l'année ;
        2. hors type de date admis, **rien** ne prouve l'année : une année de dépôt
           légal (`DL 2026`) est déclarée par l'éditeur avant l'impression, et la
           notice des tomes 2 et 4 en porte une alors que son type de date dit
           précisément « inconnue ». La promouvoir en preuve ferait entrer en
           librairie des volumes qui n'y sont pas ;
        3. type admis : `214$d` quand il nomme une année, sinon la Date 1 de `100$a`.
        """
        if self.proven_day is not None:
            return self.proven_day.year
        if self.date_type not in ADMITTED_DATE_TYPES:
            return None
        return self.declared_publication_year or self.declared_year


@dataclass(frozen=True)
class TomeAdmission:
    """Verdict d'admission d'un tome, avec son motif quand il est refusé."""

    reason: TomeRejection | None = None

    @property
    def accepted(self) -> bool:
        return self.reason is None

    @property
    def other_work(self) -> bool:
        """La notice décrit un autre ouvrage : la ligne ne reste pas au récapitulatif."""
        return self.reason in _OTHER_WORK_REASONS


def admit_tome(
    *,
    isbn13: str | None,
    notice: TomeNotice | None,
    series_title: str | None,
    author_hint: str | None = None,
    number: int | None = None,
    reference_publisher: str | None = None,
    today: date | None = None,
) -> TomeAdmission:
    """Admet un tome, ou nomme le motif du refus.

    L'ordre des contrôles dit la précision du motif : on établit d'abord **de quel
    ouvrage** parle la notice (série, numéro, auteur, éditeur), puis seulement si
    sa parution est prouvée. Un refus pour « date non prouvée » ne doit pas
    couvrir une notice qui décrit un autre livre : le motif affiché serait faux,
    alors qu'il est la seule chose que le récapitulatif retient du tome écarté.

    `today` est requis pour juger « déjà paru » : un appelant qui l'omet juge la
    parution contre le jour réel, jamais contre une date de référence inventée.
    """
    code = isbn_from_identifier(isbn13)
    if code is None or not is_valid_isbn(code):
        return TomeAdmission("invalid_isbn")
    if notice is None:
        return TomeAdmission("no_bnf_notice")
    if not _belongs_to_series(notice, series_title):
        return TomeAdmission("series_mismatch")
    if number is not None and notice.number is not None and notice.number != number:
        return TomeAdmission("tome_number_inconsistent")
    if author_hint and not _same_author(notice.authors, author_hint):
        return TomeAdmission("author_mismatch")
    if (reference_publisher and notice.publisher
            and _publishing_house(notice.publisher) != _publishing_house(reference_publisher)):
        return TomeAdmission("publisher_mismatch")
    reference_day = today or date.today()
    published_on = notice.proven_day
    if published_on is not None:
        if published_on > reference_day:
            return TomeAdmission("not_published_yet")
        return TomeAdmission()
    published_year = notice.proven_year
    if published_year is None:
        return TomeAdmission("date_unproven")
    if published_year > reference_day.year:
        return TomeAdmission("not_published_yet")
    return TomeAdmission()


def _belongs_to_series(notice: TomeNotice, series_title: str | None) -> bool:
    """La notice décrit-elle un tome de la série demandée ?

    Deux preuves valent accord : la mention de série (`225$a`) quand elle est
    présente, et à défaut le titre de la notice (`200`), qui reprend le nom de la
    série suivi du titre du tome (« La passe-miroir les disparus du Clairdelune »).
    Beaucoup de notices n'ont pas de `225` : exiger la seule mention de série
    refuserait des tomes réels pour une zone absente, et non pour un fait contraire.
    Une série non demandée ne contredit rien.
    """
    expected = normalize_text(series_title)
    if not expected:
        return True
    statement = normalize_text(notice.series_statement)
    if statement and statement == expected:
        return True
    return expected in normalize_text(notice.title)


def _same_author(authors: tuple[str, ...], author_hint: str) -> bool:
    """Au moins une mention d'auteur recoupe la saisie, par mots significatifs.

    Même règle que `same_author` du service : les notices BNF nomment les créateurs
    dans un ordre variable (« Dabos, Christelle »), on compare donc des ensembles
    de mots, et l'absence de mention ne vaut pas accord.
    """
    expected = author_tokens(author_hint)
    if not expected:
        return False
    for author in authors:
        actual = author_tokens(author)
        if actual and (actual <= expected or expected <= actual):
            return True
    return False


def _publishing_house(name: str) -> str:
    """Mot distinctif d'une maison d'édition, pour comparer sans le bruit.

    « Gallimard Jeunesse » et « Éditions Gallimard Jeunesse » sont la même maison ;
    une mention générique (« Editions ») ne distingue plus rien et ne doit pas
    provoquer un refus.
    """
    tokens = [token for token in normalize_text(name).split() if token not in _HOUSE_NOISE]
    return tokens[0] if tokens else ""


def notice_from_bnf_record(record: dict[str, Any]) -> TomeNotice | None:
    """Projette une notice BNF déjà extraite en `TomeNotice`, ou `None` si illisible.

    Ce module reste pur : il reçoit un dictionnaire déjà lu du XML MarcXchange, il
    ne parse pas de flux. `None` signifie « la notice ne prouve rien d'exploitable »,
    ce que l'admission traduit en `no_bnf_notice` plutôt qu'en admission muette.

    Deux zones de date, deux rôles : `100$a` (`date_code`) donne l'entrée au
    catalogue, le type de date et la Date 1 ; `214$d` (`publication_field`) est la
    seule à pouvoir porter un jour de parution.
    """
    if not isinstance(record, dict):
        return None
    title = record.get("title")
    if not isinstance(title, str):
        return None
    entry = parse_catalogue_entry(record.get("date_code"))
    stated_day, stated_year = parse_publication_field(record.get("publication_field"))
    return TomeNotice(
        isbn13=isbn_from_identifier(record.get("isbn13") or record.get("isbn")),
        title=title,
        date_type=entry.date_type,
        entry_date=entry.entry_date,
        declared_year=entry.declared_year,
        publication_date=stated_day,
        declared_publication_year=stated_year,
        authors=tuple(record.get("authors") or ()),
        series_statement=record.get("series_statement"),
        number=record.get("number"),
        publisher=record.get("publisher"),
    )


@dataclass(frozen=True)
class CatalogueEntry:
    """Ce que dit la zone fixe `100$a`, et ce qu'elle ne dit pas.

    `entry_date` est la date d'**entrée de la notice au catalogue** — le jour où la
    BNF a créé la notice. Elle n'est jamais une date de parution : une réimpression
    de 1998 cataloguée en 2026 porte 2026 à cet endroit. `declared_year` est la
    Date 1 (positions 9-12), et `date_type` la position 8 qui dit ce que vaut cette
    année.
    """

    date_type: str | None = None
    entry_date: date | None = None
    declared_year: int | None = None


def parse_catalogue_entry(code: str | None) -> CatalogueEntry:
    """Lit la zone fixe `100$a` d'une notice BNF (`20260206d2026…`).

    Trois lectures, aux trois positions qui ont chacune un sens : 0-7 = entrée au
    catalogue, 8 = type de date, 9-12 = Date 1. L'année ne se cherche **jamais**
    par expression régulière dans la zone : celle-ci contient aussi la date
    d'entrée, et prendre l'une pour l'autre fait d'une notice récente un livre paru
    — c'est l'erreur que ce découpage rend impossible.
    """
    if not isinstance(code, str) or len(code) <= _DATE_TYPE_INDEX:
        return CatalogueEntry()
    character = code[_DATE_TYPE_INDEX].strip().lower() or None
    year_text = code[_YEAR_SLICE]
    year = int(year_text) if year_text.isdigit() else None
    entry_text = code[_ENTRY_SLICE]
    entry_date = None
    if len(entry_text) == 8 and entry_text.isdigit():
        try:
            entry_date = date(
                int(entry_text[:4]), int(entry_text[4:6]), int(entry_text[6:]),
            )
        except ValueError:
            entry_date = None
    return CatalogueEntry(character, entry_date, year)


_PUBLICATION_DAY_PATTERNS = (
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
    re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"),
    re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4})"),
)
_YEAR_PATTERN = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def parse_publication_field(value: str | None) -> tuple[date | None, int | None]:
    """Lit `214$d` : la date de publication telle que la BNF l'écrit.

    Rend `(jour, année)`, l'un ou l'autre pouvant manquer :

    - `« DL 2026 »`, `« 2026 »`, `« [2026] »` → année seule, pas de jour ;
    - `« DL 06/02/2026 »`, `« 2026-02-06 »` → jour réel, qui vaut mieux que l'année.

    Aucune date n'est déduite ni complétée : ce qui n'est pas écrit reste absent,
    et c'est l'appelant qui décide ce que vaut une année sans jour.
    """
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = value.strip()
    for pattern in _PUBLICATION_DAY_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groups()
        try:
            if pattern is _PUBLICATION_DAY_PATTERNS[0]:
                day = date(int(groups[0]), int(groups[1]), int(groups[2]))
            else:
                day = date(int(groups[2]), int(groups[1]), int(groups[0]))
        except ValueError:
            continue
        return day, day.year
    year = _YEAR_PATTERN.search(text)
    return None, int(year.group(1)) if year else None
