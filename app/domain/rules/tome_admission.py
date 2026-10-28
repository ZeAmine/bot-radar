"""Admission d'un tome de série : ce qui prouve qu'il existe, et ce qui l'écarte.

`add « La Passe-miroir » de Christelle Dabos` a retenu **quatre** tomes, dont deux
qui n'ont jamais paru : la sélection des tomes savait dédoublonner et prioriser,
elle ne savait pas vérifier qu'un livre est sorti. Un ISBN ne prouve donc pas
l'existence d'un tome — la BNF publie une notice dès l'annonce, avec une date de
parution **prévue**. Seule une date **certaine** (`100$a`, position 8 = `d`) et
déjà passée prouve que le volume est en librairie ; `u` (« inconnue ») ne prouve
rien, même quand le millésime est là.

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

# Position de la date dans la zone fixe `100$a` : 8 = type de date, 9-12 = année.
_DATE_TYPE_INDEX = 8
_YEAR_SLICE = slice(9, 13)
_DAY_SLICE = slice(0, 8)

# Type de date BNF : `d` = date certaine, `u` = inconnue, `m`/`n`/`q`… = encodages
# de dates partielles ou douteuses. Seul `d` autorise à dire « paru tel jour ».
CERTAIN_DATE_TYPE = "d"

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
    publication_date: date | None = None
    publication_year: int | None = None
    authors: tuple[str, ...] = ()
    series_statement: str | None = None
    number: int | None = None
    publisher: str | None = None

    @property
    def certain_date(self) -> date | None:
        """Date certaine, ou `None` : une année de type `u` n'est pas une date."""
        if self.date_type != CERTAIN_DATE_TYPE:
            return None
        if self.publication_date is not None:
            return self.publication_date
        if self.publication_year is not None:
            return date(self.publication_year, 1, 1)
        return None


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
    published_on = notice.certain_date
    if published_on is None:
        return TomeAdmission("date_unproven")
    if published_on > (today or date.today()):
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
    """
    if not isinstance(record, dict):
        return None
    title = record.get("title")
    if not isinstance(title, str):
        return None
    stamp = parse_notice_date(record.get("date_code"))
    return TomeNotice(
        isbn13=isbn_from_identifier(record.get("isbn13") or record.get("isbn")),
        title=title,
        date_type=stamp.date_type,
        publication_date=stamp.publication_date,
        publication_year=stamp.publication_year,
        authors=tuple(record.get("authors") or ()),
        series_statement=record.get("series_statement"),
        number=record.get("number"),
        publisher=record.get("publisher"),
    )


@dataclass(frozen=True)
class NoticeDate:
    """Ce que dit la zone `100$a` d'une notice : le type de date, et la date lue."""

    date_type: str | None = None
    publication_date: date | None = None
    publication_year: int | None = None


def parse_notice_date(code: str | None) -> NoticeDate:
    """Lit la zone fixe `100$a` d'une notice BNF (`20260206d2026…`).

    Trois lectures, et trois seulement : position 8 = type de date, positions 9-12
    = année, positions 0-7 = date que la notice rattache à l'ouvrage (le jour y est
    écrit). L'année ne se cherche **jamais** par expression régulière dans la zone :
    celle-ci contient aussi la date d'entrée au catalogue, et prendre l'une pour
    l'autre ferait d'une notice ancienne un livre à paraître.
    """
    if not isinstance(code, str) or len(code) <= _DATE_TYPE_INDEX:
        return NoticeDate()
    character = code[_DATE_TYPE_INDEX].strip().lower()
    year_text = code[_YEAR_SLICE]
    year = int(year_text) if year_text.isdigit() else None
    day_text = code[_DAY_SLICE]
    publication_date = None
    if len(day_text) == 8 and day_text.isdigit():
        try:
            publication_date = date(int(day_text[:4]), int(day_text[4:6]), int(day_text[6:]))
        except ValueError:
            publication_date = None
    return NoticeDate(character or None, publication_date, year)
