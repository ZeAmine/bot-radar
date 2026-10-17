"""Service d'identification et recommandation automatique de métadonnées pour les BD & Comics.

Interroge la Bibliothèque Nationale de France (BNF) via son protocole SRU standard afin d'extraire
le titre officiel, l'auteur, l'éditeur et l'ISBN-13, et de générer des mots-clés
d'inclusion/exclusion optimaux.

Règle produit : **la BNF est la seule autorité ISBN**. Un ISBN proposé par ailleurs (saisie
utilisateur ou proposition IA) n'est retenu que s'il est confirmé par une notice BNF dont le
titre correspond réellement à l'œuvre recherchée.
"""

import difflib
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
import xml.parsers.expat
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from app.core.config import settings
from app.core.constants import MatchConfidence, ProviderQueryStatus
from app.core.http_client import get_shared_http_client
from app.domain.rules.book_volume import is_collection, notice_volume, requested_volume
from app.domain.rules.series_detection import SeriesSignal, detect_series
from app.providers.google_books import (
    GoogleBooksClient,
    GoogleVolume,
    ProviderQueryResult,
)
from app.services.market_price_service import extract_tome_number_from_title
from app.utils.isbn import (
    is_valid_isbn,
    isbn10_to_isbn13,
    isbn_from_identifier,
    normalize_isbn,
)
from app.utils.text import normalize_text

logger = logging.getLogger("radar.book_metadata")

STANDARD_BD_EXCLUDES = ["roman", "poche", "anglais", "english", "dvd"]

# Similarité minimale entre le titre demandé et la notice BNF pour valider une correspondance.
TITLE_SIMILARITY_MIN = 0.75
# Similarité minimale entre deux titres pour les considérer comme deux éditions de la même œuvre.
SAME_WORK_TITLE_SIMILARITY_MIN = 0.9
# Nombre maximum d'éditions alternatives remontées à l'utilisateur.
MAX_ALTERNATIVE_EDITIONS = 3
# Nombre maximum de tomes rattachés à une série suivie par son nom. Un plafond
# protège le catalogue des notices voisines ; à 6, une série normale de BD en
# perdait (Blacksad compte 7 tomes). 12 couvre les séries réelles sans ouvrir la
# porte au bruit : le préfixe de titre et la cohérence d'auteur restent le filtre.
MAX_SERIES_BOOKS = 12

# Préfixes ISBN des éditions francophones (règle GF pour la langue française).
FRENCH_ISBN_PREFIXES = ("9782", "979")

# Identifiants des autorités, tels que `BookMetadata.source` les déclare. Les possède
# le service qui les interroge : un appelant qui les recopierait finirait par annoncer
# « ni la BNF ni OpenLibrary » le jour où un troisième repli entre en scène — ce qui est
# arrivé (ADR-033).
AUTORITE_BNF = "bnf"
AUTORITE_OPENLIBRARY = "openlibrary"
AUTORITE_GOOGLE_BOOKS = "google_books"

_TITLE_QUERY_ALLOWED_CHARS = r"[^a-zA-Z0-9\sàâäéèêëîïôöùûüçÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ-]"

# Concordance entre l'indice d'auteur saisi et la notice retenue : sans indice, l'indice
# ne tranche rien (« none ») ; il désigne la notice (« matched ») ; il la contredit
# (« conflicts ») et la notice n'est alors pas l'œuvre demandée.
AuthorHintMatch = Literal["none", "matched", "conflicts"]


@dataclass
class BookMetadata:
    """Métadonnées enrichies d'une œuvre BD."""

    title: str
    author: str | None = None
    publisher: str | None = None
    isbn13: str | None = None
    recommended_includes: list[str] = field(default_factory=list)
    recommended_excludes: list[str] = field(default_factory=list)
    recommended_variants: list[str] = field(default_factory=list)
    confidence: MatchConfidence = MatchConfidence.HIGH
    source: str = AUTORITE_BNF
    publication_year: int | None = None
    format_detail: str | None = None
    # Titre de la notice portant son numéro de tome, tel que la détection de série le
    # lit (« … Tome 2 »). `None` quand la notice n'en porte aucun. C'est un titre de
    # conservé pour détecter les séries et vérifier le volume demandé ; la
    # similarité de l’œuvre reste calculée sur `title`, le titre de base.
    volume_title: str | None = None
    volume_number: int | None = None
    language: str | None = None
    declared_volume_count: int | None = None

    def __post_init__(self) -> None:
        if self.volume_number is None:
            self.volume_number = notice_volume(self.volume_title or self.title)


@dataclass(frozen=True)
class SeriesVolume:
    """Un tome et l'édition qui le décrit ; un candidat n'est jamais un ISBN confirmé."""

    number: int
    notice: BookMetadata
    isbn13: str | None = None

    @classmethod
    def from_stored(cls, row: dict[str, Any]) -> "SeriesVolume":
        """Relit une preuve du brouillon, sans donner valeur de preuve à un candidat."""
        meta = BookMetadata(
            title=row.get("title") or "", author=row.get("author"),
            publisher=row.get("publisher"), language=row.get("language"),
            source=row.get("source") or "", publication_year=row.get("publication_year"),
            format_detail=row.get("format_detail"), volume_number=row["number"],
        )
        code = isbn_from_identifier(row.get("isbn13")) if row.get("confirmed") else None
        return cls(row["number"], meta, code)

    def stored(self) -> dict[str, Any]:
        """Projection JSON pour le brouillon, distincte des éditions concurrentes."""
        return {
            "number": self.number, "title": self.notice.volume_title or self.notice.title,
            "isbn13": self.isbn13, "confirmed": self.isbn13 is not None,
            "author": self.notice.author, "publisher": self.notice.publisher,
            "language": self.notice.language, "source": self.notice.source,
            "publication_year": self.notice.publication_year,
            "format_detail": self.notice.format_detail,
        }


def _declared_series_count(title: str) -> int | None:
    """Un total explicitement annoncé par une notice, jamais déduit des numéros vus."""
    found = re.search(r"\b(\d{1,3})\s+(?:tomes|volumes|livres)\b", title, re.I)
    return int(found.group(1)) if found else None


def select_series_volumes(
    candidates: list[SeriesVolume], *, publisher: str | None = None,
) -> tuple[SeriesVolume, ...]:
    """Une édition par numéro, français confirmé d'abord, sans mélanger ses prix."""
    def priority(volume: SeriesVolume) -> tuple[bool, bool, bool, bool, bool, str, str]:
        meta = volume.notice
        return (
            volume.isbn13 is None, meta.language != "fr", meta.source != AUTORITE_BNF,
            not (publisher and meta.publisher == publisher), meta.publication_year is None,
            volume.isbn13 or "", meta.volume_title or meta.title,
        )

    per_number: dict[int, SeriesVolume] = {}
    seen: set[str] = set()
    normalized = [replace(v, isbn13=isbn_from_identifier(v.isbn13)) for v in candidates]
    for candidate in sorted(normalized, key=priority):
        code = isbn_from_identifier(candidate.isbn13)
        if code and code in seen:
            continue
        if code:
            seen.add(code)
        per_number.setdefault(candidate.number, replace(candidate, isbn13=code))
    return tuple(per_number[number] for number in sorted(per_number))


class EditionAlternative(BaseModel):
    """Édition d'une même œuvre, proposée au choix de l'utilisateur avant création."""

    isbn13: str
    publisher: str | None = None
    publication_year: int | None = None
    format_detail: str | None = None
    volume_number: int | None = None
    language: str | None = None


class WorkAlternative(BaseModel):
    """Autre ouvrage portant le même titre, proposé au choix de l'utilisateur."""

    isbn13: str
    title: str
    author: str | None = None
    publisher: str | None = None
    publication_year: int | None = None


@dataclass
class BookResolution:
    """Résultat d'une résolution d'œuvre : l'édition de référence et ses variantes.

    `other_works` porte ce que `_is_same_work` écarte : les notices qui portent
    exactement le même titre pour un **auteur différent**. Ce ne sont pas des
    éditions concurrentes, ce sont d'autres ouvrages — « Les Enfants perdus » existe
    en roman de 1989 et en BD de 2025. Les écarter en silence faisait passer le
    roman pour l'œuvre demandée, avec son ISBN, son auteur et son éditeur dans le
    récap.

    `notice_titles` porte les titres que l'autorité a **réellement rendus** pour
    cette recherche, notice retenue en tête, y compris ceux que ni `alternatives` ni
    `other_works` ne retiennent. Une notice numérotée voisine (« Absolute Martian
    Manhunter, Vol. 2 » quand on demande la série) n'est ni une édition du même
    ouvrage ni un homonyme : elle était jetée, et sans elle une œuvre numérotée
    passait pour un one-shot — l'alerte partait avec l'ISBN d'un tome
    (`series_signal`).
    """

    best: BookMetadata
    alternatives: list[BookMetadata] = field(default_factory=list)
    other_works: list[BookMetadata] = field(default_factory=list)
    author_hint_match: AuthorHintMatch = "none"
    notice_titles: tuple[str, ...] = ()

    def series_signal(self, query_title: str) -> SeriesSignal:
        """La résolution prouve-t-elle une œuvre numérotée que la saisie ne demande pas ?

        Seul point d'entrée de la détection de série : il porte la garde d'ambiguïté,
        donc aucune des deux voies d'ajout ne peut l'oublier. Une résolution ambiguë ne
        prouve rien — les notices qu'elle hésite à départager sont des homonymes, pas
        des tomes.
        """
        if self.is_ambiguous:
            return SeriesSignal()
        return detect_series(query_title=query_title, notices=self.notice_titles)

    @property
    def is_ambiguous(self) -> bool:
        """Le titre ne suffit pas à nommer l'œuvre : aucune fiche n'est confirmée."""
        if self.author_hint_match == "conflicts":
            return True
        if not self.other_works:
            return False
        # Un indice d'auteur qui désigne la notice retenue tranche l'homonymie : les
        # autres notices ne sont plus des candidates, elles ne sont que des homonymes.
        return self.author_hint_match != "matched"

    def work_choices(self) -> list[WorkAlternative]:
        """Œuvres homonymes proposées à l'utilisateur, la notice retenue en tête.

        Tant que l'utilisateur n'a pas choisi, rien n'est rattaché : une liste de
        propositions vaut mieux qu'une fiche fausse affichée comme confirmée.
        """
        works = [self.best, *self.other_works]
        choices: list[WorkAlternative] = []
        seen: set[str] = set()
        for meta in works:
            if not meta.isbn13 or meta.isbn13 in seen:
                continue
            seen.add(meta.isbn13)
            choices.append(
                WorkAlternative(
                    isbn13=meta.isbn13,
                    title=meta.title,
                    author=meta.author,
                    publisher=meta.publisher,
                    publication_year=meta.publication_year,
                )
            )
        return choices

    def editions(self) -> list[EditionAlternative]:
        """Toutes les éditions de l'œuvre, dédupliquées, édition de référence en tête.

        L'ISBN choisi contraint la recherche Vinted, le matching et la cote de marché :
        l'utilisateur doit donc pouvoir comparer les éditions avant de valider, y
        compris revenir sur celle qui a été retenue au départ.
        """
        seen: set[str] = set()
        editions: list[EditionAlternative] = []
        for meta in [self.best, *self.alternatives]:
            if not meta.isbn13 or meta.isbn13 in seen:
                continue
            seen.add(meta.isbn13)
            editions.append(
                EditionAlternative(
                    isbn13=meta.isbn13,
                    publisher=meta.publisher,
                    publication_year=meta.publication_year,
                    format_detail=meta.format_detail,
                    volume_number=meta.volume_number,
                    language=meta.language,
                )
            )
        return editions


@dataclass
class _OpenLibraryWork:
    """Notice OpenLibrary et ISBN des éditions regroupées dans le même document."""

    meta: BookMetadata
    edition_isbns: list[str] = field(default_factory=list)


# Les quatre issues d'une recherche d'ISBN, disjointes et exhaustives. `CONTRADICTION`
# se distingue d'un silence : c'est une preuve, pas une absence.
IsbnOutcome = Literal["confirme", "contradiction", "inconnu", "silence"]

_A_TRONCHE: tuple[IsbnOutcome, ...] = ("confirme", "contradiction")


@dataclass(frozen=True)
class IsbnVerdict:
    """Ce qu'une recherche d'ISBN a donné, et qui a été consulté.

    Un seul champ porte le fond : `notice` est la notice de l'autorité qui a répondu,
    `panne` le motif d'un silence. Les champs qui ne portent rien admettent, eux, des
    combinaisons contradictoires — c'est ce que trois champs facultatifs avaient
    produit, un rapport affichant « REPLI … aucune autorité a répondu » au-dessus de
    « conforme aux notices ». Ici `outcome` décide de ce que valent les autres, et
    `__post_init__` refuse ce qui contredit ce verdict.

    `consulted` est écrit par le service qui fait les appels, dans l'ordre où il les
    fait : « ni la BNF ni OpenLibrary » ne peut donc pas mentir sur une consultation
    qu'il n'a pas eu lieu. Un code mal formé n'a été soumis à personne, et la liste
    est vide — ce que l'appelant doit pouvoir dire, pas supposer.

    Sur `silence`, `notice` peut porter une réponse de repli : elle est alors
    explicitement sans valeur de confirmation, et c'est ce que l'issue dit.
    """

    outcome: IsbnOutcome
    notice: BookMetadata | None = None
    consulted: tuple[str, ...] = ()
    panne: str | None = None

    def __post_init__(self) -> None:
        if self.outcome in _A_TRONCHE:
            if self.notice is None:
                raise ValueError(f"{self.outcome} : l'autorité a tranché, la notice est requise")
            if self.panne is not None:
                raise ValueError(
                    f"{self.outcome} : une autorité a tranché, ce n'est pas un silence"
                )
        elif self.outcome == "silence":
            if self.panne is None:
                raise ValueError("silence : le motif de l'absence de réponse est requis")
        elif self.notice is not None or self.panne is not None:
            raise ValueError(f"{self.outcome} : personne n'a répondu, il n'y a rien à rendre")

    @property
    def a_tranche(self) -> bool:
        """Une autorité a-t-elle nommé un ouvrage pour cet ISBN ?"""
        return self.outcome in _A_TRONCHE

    @property
    def confirme(self) -> bool:
        """L'autorité a-t-elle nommé l'ouvrage demandé ?"""
        return self.outcome == "confirme"

    @property
    def autorite(self) -> str | None:
        """L'autorité qui a tranché, telle que sa notice la déclare ; sinon aucune."""
        return self.notice.source if self.a_tranche and self.notice is not None else None


# Types de recherche distingués par les tentatives : `title` est la requête
# `bib.title`, `title_anywhere` son élargissement, `free` le repli libre Google
# (sans opérateur), `isbn` toute recherche par code. Un repli libre n'existe
# que pour Google, jamais pour un ISBN seul.
BibliographicSearchType = Literal["isbn", "title", "title_anywhere", "free"]


@dataclass(frozen=True)
class ProviderAttempt:
    """Une interrogation réellement tentée auprès d'une autorité.

    Un provider n'est compté comme consulté que si une requête est
    effectivement partie, même en échec : `disabled` (clé absente) n'apparaît
    donc jamais ici. `has_results` dit si la réponse brute contenait des
    éléments, avant parsing, scoring et filtrage. `error_code` est un code
    contrôlé (`timeout`, `http_429`, …), jamais un message d'exception.
    """

    provider: str
    search_type: BibliographicSearchType
    status: ProviderQueryStatus
    has_results: bool
    error_code: str | None = None


@dataclass(frozen=True)
class CatalogueVerdict:
    """Ce qu'une recherche de catalogue a appris : qui a parlé, et ce qui en est sorti.

    `consulted` nomme les autorités réellement interrogées, dans l'ordre réel et
    sans doublons ; `unavailable` celles dont l'échec est établi ; `notices` les
    notices retenues. Un `notices` vide ne dit pas la même chose selon
    `unavailable` : « toutes les autorités consultées ont répondu, aucune ne
    connaît l'ouvrage » est une information ; « une autorité s'est tue » en est
    l'absence. C'est la règle pure `is_catalogue_exhausted` qui tranche — jamais
    l'appelant, qui ne voit qu'une liste vide.

    Le vocabulaire est celui du rapport de recherche (`consulted`,
    `unavailable`, `ProviderAttempt`) : le jour où le contrat partagé saura
    composer une série, l'enrichissement de fond s'y branchera sans seconde
    interprétation à maintenir.
    """

    consulted: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    notices: tuple[BookMetadata, ...] = ()
    volumes: tuple[SeriesVolume, ...] = ()
    truncated: bool = False
    declared_volume_count: int | None = None
    inconsistent_counts: bool = False
    rejected_isbns: tuple[str, ...] = ()

    @property
    def notice(self) -> BookMetadata | None:
        """La notice retenue, s'il y en a une (une recherche par titre en rend au plus une)."""
        return self.notices[0] if self.notices else None


def consulted_authorities(attempts: tuple[ProviderAttempt, ...]) -> tuple[str, ...]:
    """Autorités réellement interrogées, ordre réel, sans doublons.

    Un désactivé n'y figure pas : une source sans clé n'a jamais été interrogée,
    et l'annoncer consultée serait un mensonge de plus.
    """
    seen: list[str] = []
    for attempt in attempts:
        if attempt.provider not in seen:
            seen.append(attempt.provider)
    return tuple(seen)


def failed_authorities(attempts: tuple[ProviderAttempt, ...]) -> tuple[str, ...]:
    """Autorités dont l'échec est établi (un désactivé n'est jamais en échec)."""
    return tuple(
        provider
        for provider in consulted_authorities(attempts)
        if any(
            attempt.provider == provider
            and attempt.status != ProviderQueryStatus.SUCCESS
            for attempt in attempts
        )
    )


def _verdict(
    attempts: list[ProviderAttempt], *notices: BookMetadata | None
) -> CatalogueVerdict:
    """Le verdict d'une recherche : les tentatives réelles et les notices retenues.

    Une notice `None` (aucun repli n'a rendu) n'est pas une notice : elle est
    filtrée ici, pour qu'aucun appelant n'ait à se demander si le tuple en porte.
    """
    return CatalogueVerdict(
        consulted=consulted_authorities(tuple(attempts)),
        unavailable=failed_authorities(tuple(attempts)),
        notices=tuple(notice for notice in notices if notice is not None),
    )


@dataclass(frozen=True)
class BibliographicReport:
    """Enveloppe d'une recherche : le résultat métier et les tentatives réelles.

    `BookResolution` n'a pas à représenter une absence totale de candidat :
    `resolution=None` + `attempts` le fait. Un rapport = un chemin : `title`
    porte une résolution (éventuellement `None`), `isbn` porte toujours un
    verdict — les deux ne se mélangent jamais.
    """

    kind: Literal["title", "isbn"]
    resolution: BookResolution | None = None
    verdict: IsbnVerdict | None = None
    attempts: tuple[ProviderAttempt, ...] = ()

    def __post_init__(self) -> None:
        if self.kind == "title" and self.verdict is not None:
            raise ValueError("un rapport titre porte une résolution, pas de verdict")
        if self.kind == "isbn" and (self.verdict is None or self.resolution is not None):
            raise ValueError("un rapport ISBN porte un verdict, jamais de résolution")


def _attempt(
    provider: str,
    search_type: BibliographicSearchType,
    result: ProviderQueryResult[Any],
) -> ProviderAttempt:
    """Une tentative depuis un résultat typé : statut, brut et code contrôlé."""
    return ProviderAttempt(
        provider=provider,
        search_type=search_type,
        status=result.status,
        has_results=result.has_results,
        error_code=result.error_code,
    )


OPENLIBRARY_SOURCE_NOTE = "Source OpenLibrary (repli BNF)"
GOOGLE_BOOKS_SOURCE_NOTE = "Source Google Books (repli BNF)"

# Provenance par autorité secondaire. Une table plutôt qu'un cas particulier : la
# troisième source a montré qu'un `if` unique se contentait de taire la suivante, et
# une notice de repli affichée sans sa provenance se lit comme une confirmation BNF.
PROVENANCE_NOTES: dict[str, str] = {
    AUTORITE_OPENLIBRARY: OPENLIBRARY_SOURCE_NOTE,
    AUTORITE_GOOGLE_BOOKS: GOOGLE_BOOKS_SOURCE_NOTE,
}


def provenance_note(meta: BookMetadata) -> str | None:
    """Signale au récap une donnée d'identité issue d'une autorité secondaire."""
    return PROVENANCE_NOTES.get(meta.source)


def ambiguity_note(resolution: BookResolution) -> str | None:
    """Dit pourquoi aucune fiche n'est rattachée quand le titre désigne plusieurs œuvres.

    Sans ce texte, l'absence d'auteur et d'éditeur se lit comme un oubli. Une fiche
    d'homonyme affichée sans avertissement, elle, se lit comme une confirmation.
    """
    if not resolution.is_ambiguous:
        return None
    found = resolution.best
    if resolution.author_hint_match == "conflicts":
        found_author = found.author or "un auteur non renseigné"
        return (
            f"⚠️ La BNF connaît « {found.title} » de {found_author}, "
            "pas l'ouvrage demandé."
        )
    others = len(resolution.other_works)
    if others == 1:
        return f"⚠️ « {found.title} » : un autre ouvrage porte ce titre."
    return f"⚠️ « {found.title} » : {others} autres ouvrages portent ce titre."


def clean_openlibrary_author(raw_author: str) -> str:
    """Retire la mention de rôle éventuelle d'un nom d'auteur OpenLibrary."""
    cleaned = re.sub(r"\s*\(.*?\)\s*", " ", raw_author).strip(" ,")
    return cleaned or raw_author.strip()


def title_similarity(first: str | None, second: str | None) -> float:
    """Compare deux titres normalisés (0.0 = aucun rapport, 1.0 = titres identiques)."""
    norm_a = normalize_text(first)
    norm_b = normalize_text(second)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
    # Un sous-titre ou un suffixe d'auteur côté BNF ne doit pas faire tomber la similarité,
    # mais seulement s'il reste marginal par rapport au titre (sinon c'est un autre ouvrage).
    shorter, longer = sorted((norm_a, norm_b), key=len)
    if shorter in longer and len(shorter) / len(longer) >= 0.75:
        ratio = max(ratio, SAME_WORK_TITLE_SIMILARITY_MIN)
    return ratio


def normalize_edition_language(raw: str | None) -> str | None:
    """Normalise les codes attestés par une notice d'édition, sans les deviner."""
    if not raw:
        return None
    code = raw.strip().lower().rsplit("/", 1)[-1]
    if code in {"und", "mul", "zxx"}:
        return None
    return {"eng": "en", "fre": "fr", "fra": "fr", "en": "en", "fr": "fr"}.get(
        code, code if re.fullmatch(r"[a-z]{2,3}", code) else None
    )


def matches_requested_volume(meta: BookMetadata, title: str) -> bool:
    """Une notice doit prouver le numéro demandé et désigner un volume individuel."""
    number = requested_volume(title)
    return number is None or (
        meta.volume_number == number
        and not is_collection(meta.volume_title or meta.title)
    )


def format_edition_fragments(format_detail: str | None) -> str:
    """Fragments chiffrés d'une notice de format (« 224 p. · 24 cm »).

    La BNF décrit le format de façon bavarde (« 1 vol. (224 p.) : ill. en coul. ;
    24 cm ») : on ne retient que la pagination et les dimensions, seules informations
    discriminantes pour un humain. Les mentions purement décoratives de la notice
    (« ill. en coul. ») sont du bruit.
    """
    parts: list[str] = []
    for chunk in re.split(r"[;:]", format_detail or ""):
        cleaned = re.sub(r"^\d+\s*vol\.\s*", "", chunk.strip())
        cleaned = cleaned.replace("(", "").replace(")", "").strip(" ,")
        if cleaned and any(char.isdigit() for char in cleaned):
            parts.append(cleaned)
    return " · ".join(parts)


def same_author(first: str | None, second: str | None) -> bool:
    """Indique si deux notices se rapportent au même auteur (comparaison par mots significatifs).

    Les notices BNF presents les createurs sous des formes heterogenes
    (« Panchaud, Martin », « Martin Panchaud », rôles inclus) : on compare donc
    les mots significatifs plutot que les chaînes brutes.
    """
    tokens_a = {token for token in normalize_text(first).split() if len(token) >= 3}
    tokens_b = {token for token in normalize_text(second).split() if len(token) >= 3}
    return bool(tokens_a & tokens_b)


def authors_conflict(first: str | None, second: str | None) -> bool:
    first_words = set(normalize_text(first).split())
    second_words = set(normalize_text(second).split())
    return bool(first_words and second_words
                and not (first_words <= second_words or second_words <= first_words))


def clean_title_query(title: str) -> str:
    """Normalise un titre saisi pour la requête CQL de la BNF."""
    cleaned = re.sub(_TITLE_QUERY_ALLOWED_CHARS, " ", title).strip()
    return cleaned or title


# La liste blanche latine des titres mutile un auteur : « Juan Díaz Canales »
# devient « Juan D az Canales », et le SRU ne rend plus rien. Seuls les
# caractères qui cassent la syntaxe CQL sont retirés, jamais une lettre.
_CQL_FORBIDDEN_CHARS = re.compile(r'["\r\n\t]+')
_CQL_SPACES = re.compile(r"\s+")


def clean_author_query(author: str) -> str:
    """Rend un terme auteur sûr pour une valeur CQL, sans mutiler les lettres."""
    without_forbidden = _CQL_FORBIDDEN_CHARS.sub(" ", author)
    return _CQL_SPACES.sub(" ", without_forbidden).strip()


class _PrologCompleteError(Exception):
    """Interrompt la lecture : la question posée au prolog a reçu sa réponse."""


def _declares_doctype(payload: bytes) -> bool:
    """Vrai si le document *déclare* une DOCTYPE, et non s'il en parle.

    Le garde précédent cherchait le littéral `<!DOCTYPE` dans les octets. Cette
    recherche avait deux défauts, tous deux vérifiés :

    - elle écartait une notice dont le texte — via un CDATA — mentionne
      `<!DOCTYPE html`, et annonçait une panne sur une réponse lisible ;
    - elle laissait passer l'inverse : une réponse en UTF-16 ne contient pas ces
      octets-là, et une entité interne y était donc développée sans être vue.

    Une recherche d'octets ne voit pas la structure du document. `expat` la voit.

    La lecture s'arrête au premier élément racine : une déclaration DOCTYPE est
    nécessairement dans le prolog, donc avant lui. Un document ordinaire ne coûte
    ainsi que son en-tête, et non un second parcours de toutes ses notices. Le
    gestionnaire `StartDoctypeDeclHandler` s'exécute quant à lui quand la
    déclaration est lue, donc avant que le sous-ensemble interne ne soit traité —
    la garde ne dépend plus d'un parseur qui refuserait les entités par accident
    d'implémentation.

    Un document malformé n'est pas une déclaration : il rend `False`, et le
    parseur de la ligne suivante rendra la panne, avec son motif.
    """
    declaree = False
    parser = xml.parsers.expat.ParserCreate()

    def _lu(_name: str, *_args: object) -> None:
        nonlocal declaree
        declaree = True
        raise _PrologCompleteError

    def _racine(_name: str, _attrs: dict[str, str]) -> None:
        raise _PrologCompleteError

    parser.StartDoctypeDeclHandler = _lu
    parser.StartElementHandler = _racine
    try:
        parser.Parse(payload, True)
    except _PrologCompleteError:
        pass
    except xml.parsers.expat.ExpatError:
        return False
    return declaree


def _parse_year(raw_date: str) -> int | None:
    """Extrait l'année de publication d'une date BNF (ex: '2022', '2022-09', 'c2022')."""
    match = re.search(r"(1[89]\d{2}|20\d{2})", raw_date)
    return int(match.group(1)) if match else None


def parse_query_title_and_author(query: str) -> tuple[str, str | None]:
    """Sépare intelligemment le titre et un éventuel indice d'auteur dans la saisie utilisateur.

    Exemples :
    - 'deathbringer de ismael le Grand' -> ('deathbringer', 'ismael le Grand')
    - 'la saga de grimr de Jeremy Morrow' -> ('la saga de grimr', 'Jeremy Morrow')
    - 'La bête par Chabouté' -> ('La bête', 'Chabouté')
    - 'Blacksad' -> ('Blacksad', None)
    """
    text = query.strip()

    # Découpage explicite par "par" ou "by"
    parts = re.split(r"\s+(?:par|by)\s+", text, flags=re.IGNORECASE)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), _clean_author_candidate(parts[1])

    for separator in re.finditer(r"\s+de\s+", text, flags=re.IGNORECASE):
        suffix = text[separator.end():].strip()
        prefix = text[:separator.start()].strip()
        words = suffix.split()
        if not prefix or not 1 <= len(words) <= 5:
            continue
        if text[separator.start():separator.end()].strip() == "De":
            suffix = "De " + suffix
        if words[0][:1].isupper() or (
            not re.search(r"\s+de\s+", suffix, re.IGNORECASE)
            and any(word[:1].isupper() for word in words)
        ):
            return prefix, _clean_author_candidate(suffix)

    return text, None


def _clean_author_candidate(raw_author: str) -> str | None:
    """Retire la ponctuation de fin de phrase qui entoure un nom d'auteur.

    « les enfants perdus de Nef. » donne « Nef. » : le point final ferait échouer
    toute concordance d'auteur, donc l'homonymie resterait indétectée.
    """
    cleaned = raw_author.strip(" .,;:!? ")
    return cleaned or None


def clean_bnf_title(raw_title: str) -> str:
    """Nettoie le titre brut retourné par la BNF
    (suppression des mentions d'auteur/sous-titres longs)."""
    t = raw_title.strip()
    if " / " in t:
        t = t.split(" / ")[0].strip()
    if " : " in t:
        parts = t.split(" : ")
        if len(parts[0].strip()) >= 3:
            t = parts[0].strip()
    return t


# Marqueur explicite de tome, cherché **en fin** de sous-titre (« 150 recettes et
# techniques. Tome 1 »). Le `\b` devant la lettre évite de confondre le « t » d'un mot
# avec un marqueur, et un simple nombre en fin de sous-titre (« … 2022 ») n'en est pas
# un : seule une forme nommée décrit un tome.
_TOME_MARKER = re.compile(
    r"\b(?:tomes?|t|vols?|volumes?|livres?|books?|parties?|parts?)\s*\d{1,4}\b\s*$",
    re.IGNORECASE,
)


def clean_bnf_series_title(raw_title: str) -> str:
    """Titre de la notice avec son numéro de tome, quand le sous-titre le portait.

    `clean_bnf_title` coupe tout ce qui suit « : » — sous-titre **et** numéro de tome :
    « Manuel du cuisinier amateur : 150 recettes et techniques. Tome 1 » devenait
    « Manuel du cuisinier amateur », indistinguable du tome 2. Deux tomes passaient
    alors pour deux éditions d'un one-shot, et l'alerte partait avec l'ISBN de l'un
    d'eux (ADR-025).

    Ici, le titre de base reste celui de `clean_bnf_title` — c'est lui qui porte
    l'affichage, le score de similarité et le filtre de candidats — et seul le marqueur
    de tome explicitement présent dans le sous-titre lui est ré-appendé. Un sous-titre
    sans marqueur rend le titre de base inchangé.
    """
    base = clean_bnf_title(raw_title)
    without_author = raw_title.strip()
    if " / " in without_author:
        without_author = without_author.split(" / ")[0].strip()
    prefix, separator, subtitle = without_author.partition(" : ")
    # Base trop courte pour nommer l'œuvre : `clean_bnf_title` a gardé le titre entier,
    # il n'y a rien à ré-appender sans le dupliquer.
    if not separator or len(prefix.strip()) < 3:
        return base
    marker = _TOME_MARKER.search(subtitle.strip())
    if not marker:
        return base
    return f"{base} {marker.group(0).strip()}"


def clean_bnf_creator(raw_creator: str) -> str:
    """Formate le créateur BNF ('Nom, Prénom (dates). Fonction' -> 'Prénom Nom').

    Le prénom s'arrête au premier millésime ou au rôle BNF : « Nicolas. Auteur du
    texte » n'est pas un prénom, et un créateur non parenthésé (« Fébule, Nicolas.
    Auteur du texte ») laissait ce rôle collé au nom, jusque dans les boutons.
    """
    m = re.match(r"^([^,]+),\s*([^(]+)", raw_creator)
    if m:
        last = m.group(1).strip()
        first = re.split(r"[.(]", m.group(2))[0].strip()
        return f"{first} {last}".strip()
    return re.sub(r"\(.*?\).*|\..*", "", raw_creator).strip()


def clean_bnf_publisher(raw_publisher: str) -> str:
    """Nettoie l'éditeur BNF (ex: 'Éditions Delcourt (Paris)' -> 'Éditions Delcourt')."""
    return re.sub(r"\(.*?\)", "", raw_publisher).strip()


def generate_recommended_keywords(
    title: str, _author: str | None = None
) -> tuple[list[str], list[str], list[str]]:
    """Génère des mots-clés d'inclusion, d'exclusion et de variantes adaptés à l'œuvre.

    **Une seule inclusion**, jamais deux formulations du même titre. La branche
    historique ajoutait aussi la version sans article : le matcher exigant **toutes**
    les inclusions (`all(...)`), la seconde ne pouvait rien élargir et la première
    imposait l'article — « Couleur des choses » était refusé pour « La couleur des
    choses ». On retient donc la forme **sans article** quand elle reste multi-mots :
    la recherche est une phrase en mots entiers, donc cette forme est trouvée aussi
    bien dans « Couleur des choses » que dans « La couleur des choses ». Un titre dont
    l'article retiré ne laisse qu'un mot (« Le Chat » → « chat ») garde sa forme
    complète : on n'élargit jamais la veille à un mot isolé.
    """
    norm_title = normalize_text(title)
    includes: list[str] = []

    if norm_title:
        includes.append(norm_title)

        # La forme sans article est une **alternative** du même titre, pas un critère de
        # plus : elle remplace la forme complète, et seulement si elle reste distincte.
        for article in ["la ", "le ", "les ", "l "]:
            if norm_title.startswith(article):
                without_article = norm_title[len(article):].strip()
                if len(without_article.split()) >= 2:
                    includes = [without_article]
                break

    # Variantes automatiques (ex: "deathbringer" -> "death bringer")
    variants: list[str] = []
    compound_words = {
        "deathbringer": "death bringer",
        "hardboiled": "hard boiled",
        "lowreader": "low reader",
    }
    for comp, var in compound_words.items():
        if comp in norm_title and var not in variants and var not in includes:
            variants.append(var)

    # Exclusions standards
    excludes = list(STANDARD_BD_EXCLUDES)

    # Exclusions spécifiques selon le titre
    if "deathbringer" in norm_title:
        excludes.extend(["figurine", "warhammer"])
    if "grimr" in norm_title:
        excludes.append("islandais")

    return includes, excludes, variants


class BookMetadataService:
    """Service d'interrogation et de recommandation de métadonnées de livres."""

    BNF_SRU_URL = "https://catalogue.bnf.fr/api/SRU"
    OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
    OPENLIBRARY_FIELDS = (
        "title,author_name,publisher,first_publish_year,isbn,"
        "number_of_pages_median,language"
    )
    OPENLIBRARY_MAX_RESULTS = 10

    def __init__(
        self,
        timeout_seconds: float = 8.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.http_client = http_client

    async def lookup(self, query: str) -> BookMetadata | None:
        """Identifie une œuvre BD à partir d'un titre, ISBN ou URL Vinted."""
        clean_q = query.strip()
        if not clean_q:
            return None

        # 1. Vérification si la saisie est directement un ISBN
        raw_digits = re.sub(r"[^0-9X]", "", clean_q.upper())
        if len(raw_digits) in (10, 13) and (raw_digits.isdigit() or raw_digits.endswith("X")):
            return await self.lookup_by_isbn(raw_digits)

        # 2. Vérification si la saisie est une URL Vinted
        if "vinted." in clean_q.lower():
            return await self._lookup_from_vinted_url(clean_q)

        # 3. Recherche par Titre (+ Auteur éventuel). `resolve_input` garde son
        # retour historique (la notice) : le verdict est lu par les appelants qui
        # savent quoi en faire, pas par les écrans de saisie.
        return (await self.lookup_by_title_and_author(clean_q)).notice

    async def resolve_input(self, query: str) -> BookResolution | None:
        """Résout une saisie libre de l'utilisateur, quelle qu'en soit la forme.

        La forme de la saisie (ISBN, URL Vinted, titre + auteur) reste une connaissance
        du service : l'appelant veut une résolution, pas une décision de forme. Un ISBN
        ou une URL nommant un ouvrage ne sont pas ambigus, d'où l'absence d'autres
        œuvres candidates.
        """
        clean_q = query.strip()
        if not clean_q:
            return None

        raw_digits = re.sub(r"[^0-9X]", "", clean_q.upper())
        if len(raw_digits) in (10, 13) and (raw_digits.isdigit() or raw_digits.endswith("X")):
            meta = await self.lookup_by_isbn(raw_digits)
            return BookResolution(best=meta) if meta else None

        if "vinted." in clean_q.lower():
            meta = await self._lookup_from_vinted_url(clean_q)
            return BookResolution(best=meta) if meta else None

        title_query, author_query = parse_query_title_and_author(clean_q)
        return await self.resolve_for_alert(title_query, author_query)

    async def lookup_by_isbn(self, isbn: str) -> BookMetadata | None:
        """Recherche une œuvre par son ISBN officiel sur la BNF.

        Seule la notice est rendue, quelle qu'ait été la réponse de l'autorité : un
        appelant qui affiche une fiche préfère une fiche absente à une fiche fausse,
        et une panne ne doit jamais ressembler à un refus. `consult_authorities`
        est le même appel pour qui doit, lui, rendre des comptes.
        """
        return (await self.consult_authorities(isbn)).notice

    async def consult_authorities(self, isbn: str) -> IsbnVerdict:
        """Interroge les autorités, dans l'ordre, et rend leur verdict nommé.

        La BNF tranche ; OpenLibrary ne parle qu'en repli, et sa réponse ne remplace
        pas la sienne : sur une panne de la BNF, elle est rendue mais vaut confirmation
        de rien. C'est aussi le seul endroit qui sait qui a été consulté, et dans quel
        ordre — un appelant ne peut donc pas annoncer des autorités qu'il n'a pas
        interrogées, ni taire un troisième repli le jour où il entre en scène.
        """
        report = await self.consult_authorities_report(isbn)
        assert report.verdict is not None
        return report.verdict

    async def consult_authorities_report(self, isbn: str) -> BibliographicReport:
        """Mêmes décisions que `consult_authorities`, tentatives réelles en plus.

        Chaque autorité réellement interrogée — y compris en échec — laisse une
        tentative ; une source non configurée n'en laisse aucune. Les verdicts
        rendus sont identiques à la méthode historique, qui y délègue.
        """
        clean_isbn = normalize_isbn(isbn)
        if not clean_isbn or not is_valid_isbn(clean_isbn):
            # Un code mal formé n'a été soumis à personne : le dire « inconnu »
            # accuserait une autorité de l'ignorer.
            return BibliographicReport(
                kind="isbn", verdict=IsbnVerdict(outcome="inconnu")
            )

        attempts: list[ProviderAttempt] = []
        bnf_result, panne = await self._bnf_records_result(
            f'bib.isbn adj "{clean_isbn}"'
        )
        attempts.append(_attempt(AUTORITE_BNF, "isbn", bnf_result))
        if bnf_result.items:
            return BibliographicReport(
                kind="isbn",
                verdict=IsbnVerdict(
                    outcome="confirme",
                    notice=self._attach_recommendations(bnf_result.items[0]),
                    consulted=(AUTORITE_BNF,),
                ),
                attempts=tuple(attempts),
            )

        consulted: tuple[str, ...] = (AUTORITE_BNF, AUTORITE_OPENLIBRARY)
        ol_works, ol_result = await self._openlibrary_works({"isbn": clean_isbn})
        attempts.append(_attempt(AUTORITE_OPENLIBRARY, "isbn", ol_result))
        if ol_works:
            return BibliographicReport(
                kind="isbn",
                verdict=IsbnVerdict(
                    outcome="silence" if panne else "confirme",
                    notice=await self._openlibrary_edition_metadata(
                        ol_works[0].meta, require_volume=True
                    ),
                    consulted=consulted,
                    panne=panne,
                ),
                attempts=tuple(attempts),
            )

        # Troisième autorité, quand elle est configurée : un livre publié il y a deux
        # semaines n'est dans aucun catalogue national (ADR-028), et c'est précisément
        # le cas que ce repli sert. Elle n'est nommée « consultée » que si elle l'a été.
        google_client = self._google_books_client()
        if google_client.is_configured:
            consulted = (*consulted, AUTORITE_GOOGLE_BOOKS)
            notice, google_result = await self._google_books_notice_result(
                google_client, clean_isbn
            )
            attempts.append(_attempt(AUTORITE_GOOGLE_BOOKS, "isbn", google_result))
            if notice is not None:
                return BibliographicReport(
                    kind="isbn",
                    verdict=IsbnVerdict(
                        outcome="silence" if panne else "confirme",
                        notice=notice,
                        consulted=consulted,
                        panne=panne,
                    ),
                    attempts=tuple(attempts),
                )

        # Aucune autorité n'a rien nommé : sans `panne`, ce serait un verdict.
        return BibliographicReport(
            kind="isbn",
            verdict=IsbnVerdict(
                outcome="silence" if panne else "inconnu",
                consulted=consulted,
                panne=panne,
            ),
            attempts=tuple(attempts),
        )

    async def verify_isbn(
        self, isbn13: str, expected_title: str, *,
        outcomes: list[IsbnVerdict] | None = None,
    ) -> BookMetadata | None:
        """Confirme qu'un ISBN désigne bien l'œuvre attendue.

        Filet anti-hallucination : un ISBN bien formé mais attribué à une autre œuvre
        (préfixe d'éditeur recombiné par un LLM, par exemple) est rejeté sur le titre.
        Une notice obtenue pendant une panne ne confirme rien.
        """
        verdict = await self.confirm_isbn_for_title(isbn13, expected_title)
        if outcomes is not None:
            outcomes.append(verdict)
        return verdict.notice if verdict.confirme else None

    async def confirm_isbn_for_title(
        self, isbn13: str, expected_title: str | None
    ) -> IsbnVerdict:
        """Le même verdict, appliqué à un ISBN saisi : désigne-t-il l'œuvre demandée ?

        `verify_isbn` ne renvoie que la notice : l'appelant ne peut pas distinguer
        « la BNF connaît cet ISBN et c'est un autre livre » de « personne ne connaît
        cet ISBN ». Ces deux verdicts n'appellent pas la même réponse : le premier est
        une contradiction, le second une simple absence de preuve. La séparation se
        fait ici, une fois pour tous.
        """
        verdict = await self.consult_authorities(isbn13)
        # Sans titre attendu, il n'y a rien à quoi comparer la notice : l'ISBN est
        # alors retenu pour ce qu'il est, sans contradiction à invoquer.
        if not expected_title or not verdict.confirme or verdict.notice is None:
            return verdict

        if (
            title_similarity(verdict.notice.title, expected_title) < TITLE_SIMILARITY_MIN
            or not matches_requested_volume(verdict.notice, expected_title)
        ):
            logger.info(
                "ISBN %s écarté : les autorités le rattachent à « %s » et non à « %s »",
                isbn13,
                verdict.notice.title,
                expected_title,
            )
            return replace(verdict, outcome="contradiction")
        return verdict

    async def resolve_for_alert(
        self, title: str, author_hint: str | None = None
    ) -> BookResolution | None:
        """Résout l'édition de référence d'une œuvre à partir d'un titre (+ auteur).

        Ne renvoie que des notices réellement proches du titre demandé et expose les autres
        éditions connues de la même œuvre pour que l'utilisateur puisse les voir.

        Un titre n'est pas une identité : « Les Enfants perdus » désigne un roman de 1989
        comme une BD de 2025. Quand plusieurs notices portent le même titre sous des
        auteurs différents, `is_ambiguous` est vrai et l'appelant ne rattache rien — il
        propose le choix. Seule une concordance d'auteur tranche l'homonymie.
        """
        return (await self.resolve_for_alert_report(title, author_hint)).resolution

    async def resolve_for_alert_report(
        self, title: str, author_hint: str | None = None
    ) -> BibliographicReport:
        """Mêmes décisions que `resolve_for_alert`, tentatives réelles en plus.

        Chaque requête réellement partie — y compris en échec — laisse une
        tentative, dans l'ordre d'exécution : la panne BNF d'un chemin titre
        n'est plus indiscernable d'une absence. Le retour historique
        (`resolve_for_alert`, utilisé par Telegram dans ce lot) est inchangé
        et y délègue : seule la lecture du rapport (nouveau contrat) distingue
        un repli pendant une panne d'une identification confirmée.
        """
        if not title.strip():
            return BibliographicReport(kind="title")

        attempts: list[ProviderAttempt] = []
        clean_title_q = clean_title_query(title)
        # Sans l'auteur, la fenêtre de dix notices se remplit d'homonymes : « Le
        # Voyage » annonce 23 823 notices et n'en rend aucune qui soit l'œuvre.
        author_q = clean_author_query(author_hint) if author_hint else ""
        if author_q:
            bnf_result, _panne = await self._bnf_records_result(
                f'bib.title all "{clean_title_q}" and bib.author all "{author_q}"',
                author_hint=author_hint,
                title_hint=clean_title_q,
            )
            attempts.append(_attempt(AUTORITE_BNF, "title", bnf_result))
            # Repli strict : seule une conjonction réussie et vide est relancée.
            # Une panne ne déclenche rien, elle ne prouve ni présence ni absence.
            if bnf_result.status == ProviderQueryStatus.SUCCESS and not bnf_result.items:
                bnf_result, _panne = await self._bnf_records_result(
                    f'bib.title all "{clean_title_q}"',
                    author_hint=author_hint,
                    title_hint=clean_title_q,
                )
                attempts.append(_attempt(AUTORITE_BNF, "title", bnf_result))
        else:
            bnf_result, _panne = await self._bnf_records_result(
                f'bib.title all "{clean_title_q}"',
                author_hint=author_hint,
                title_hint=clean_title_q,
            )
            attempts.append(_attempt(AUTORITE_BNF, "title", bnf_result))
        records = list(bnf_result.items)
        if not records and author_hint:
            anywhere_result, _anywhere_panne = await self._bnf_records_result(
                f'bib.anywhere all "{clean_title_q}"',
                author_hint=author_hint,
                title_hint=clean_title_q,
            )
            attempts.append(_attempt(AUTORITE_BNF, "title_anywhere", anywhere_result))
            records = list(anywhere_result.items)

        candidates = [
            record
            for record in records
            if title_similarity(record.title, title) >= TITLE_SIMILARITY_MIN
            and matches_requested_volume(record, title)
        ]
        only_other_authors = bool(author_hint and author_hint.strip()) and bool(candidates) and all(
            self._author_hint_match(candidate, author_hint) == "conflicts"
            for candidate in candidates
        )
        fallback_resolution: BookResolution | None = None
        if not candidates or only_other_authors:
            disputed_isbns = {candidate.isbn13 for candidate in candidates}

            def usable_fallback(resolution: BookResolution | None) -> bool:
                """Un homonyme ne justifie jamais de réattribuer son ISBN au repli."""
                return resolution is not None and (
                    not only_other_authors or (
                        resolution.author_hint_match == "matched"
                        and resolution.best.isbn13 not in disputed_isbns
                    )
                )

            # Rien d'exploitable, ou seulement d'autres auteurs : les deux replis dans
            # l'ordre. Avec des homonymes BNF, le repli doit nommer l'auteur demandé.
            ol_resolution, ol_attempt = await self._resolve_via_openlibrary(
                title, author_hint
            )
            attempts.append(ol_attempt)
            if usable_fallback(ol_resolution):
                fallback_resolution = ol_resolution
            else:
                google_resolution, google_attempts = await self._resolve_via_google_books(
                    title, author_hint
                )
                attempts.extend(google_attempts)
                if usable_fallback(google_resolution):
                    fallback_resolution = google_resolution

            if fallback_resolution is not None:
                # Les homonymes BNF restent des œuvres distinctes, jamais des éditions
                # du repli. Leur présence ne contredit pas un auteur explicitement nommé.
                homonyms = list(fallback_resolution.other_works)
                seen = {meta.isbn13 for meta in homonyms}
                for candidate in candidates:
                    if candidate.isbn13 not in seen and self._is_homonym(
                        candidate, fallback_resolution.best, title
                    ):
                        homonyms.append(candidate)
                        seen.add(candidate.isbn13)
                fallback_resolution = replace(
                    fallback_resolution,
                    other_works=homonyms[:MAX_ALTERNATIVE_EDITIONS],
                    alternatives=[
                        meta for meta in fallback_resolution.alternatives
                        if meta.isbn13 not in disputed_isbns
                    ],
                )
                return BibliographicReport(
                    kind="title",
                    resolution=fallback_resolution,
                    attempts=tuple(attempts),
                )
            if not candidates:
                return BibliographicReport(kind="title", attempts=tuple(attempts))
            # Aucun repli ne nomme l'auteur voulu : les homonymes BNF sont conservés
            # avec le bilan des tentatives, sans ratifier l'un d'eux en silence.

        candidates = self._prefer_author_hint(candidates, author_hint)
        best = self._attach_recommendations(candidates[0])
        alternatives = [
            record
            for record in candidates[1 : 1 + MAX_ALTERNATIVE_EDITIONS]
            if self._is_same_work(record, best)
        ]
        other_works = [
            record for record in candidates[1:] if self._is_homonym(record, best, title)
        ][:MAX_ALTERNATIVE_EDITIONS]
        return BibliographicReport(
            kind="title",
            resolution=BookResolution(
                best=best,
                alternatives=alternatives,
                other_works=other_works,
                author_hint_match=self._author_hint_match(best, author_hint),
                # La preuve d'une série est portée par le titre qui garde son numéro de
                # tome : la notice retenue ne l'a pas toujours, le sous-titre peut le
                # cacher.
                notice_titles=tuple(
                    dict.fromkeys(
                        record.volume_title or record.title for record in candidates
                    )
                ),
            ),
            attempts=tuple(attempts),
        )

    @staticmethod
    def _prefer_author_hint(
        candidates: list[BookMetadata], author_hint: str | None
    ) -> list[BookMetadata]:
        """Fait remonter la notice dont l'auteur correspond à l'indice saisi.

        Le score BNF ne départage deux homonymes que de quelques dixièmes : quand
        l'utilisateur a écrit « Les Enfants perdus de Nef », c'est cette concordance
        qui doit trancher, pas la position dans la réponse SRU.
        """
        if not author_hint or not author_hint.strip():
            return candidates
        for index, record in enumerate(candidates):
            if record.author and same_author(record.author, author_hint):
                if index == 0:
                    return candidates
                return [record, *candidates[:index], *candidates[index + 1 :]]
        return candidates

    @staticmethod
    def _author_hint_match(
        best: BookMetadata, author_hint: str | None
    ) -> AuthorHintMatch:
        """L'indice d'auteur désigne-t-il la notice retenue, la contredit-il, ou rien ?"""
        if not author_hint or not author_hint.strip() or not best.author:
            return "none"
        return "matched" if same_author(best.author, author_hint) else "conflicts"

    @staticmethod
    def _is_homonym(
        candidate: BookMetadata, reference: BookMetadata, requested_title: str
    ) -> bool:
        """Un autre ouvrage portant exactement le titre que l'utilisateur a demandé.

        Les trois titres doivent coïncider : « Les Enfants perdus » demandé ne peut pas
        être rendu ambigu par une notice de « Les Enfants perdus tome 2 », ni par un
        ouvrage voisin comme « De la couleur des choses ». L'ambiguïté porte sur le
        titre saisi, pas sur les notices voisines qu'il a ramenées.
        """
        wanted = normalize_text(requested_title)
        if not wanted:
            return False
        exact = normalize_text(reference.title) == wanted
        if exact and normalize_text(candidate.title) != wanted:
            return False
        if not exact and (
            title_similarity(candidate.title, requested_title) < TITLE_SIMILARITY_MIN
            or title_similarity(reference.title, requested_title) < TITLE_SIMILARITY_MIN
            or requested_volume(candidate.title) is not None
            or requested_volume(reference.title) is not None
        ):
            return False
        # Sans auteur connu des deux côtés, rien ne prouve qu'il s'agit d'un autre
        # ouvrage : la notice reste une édition possible.
        return not BookMetadataService._is_same_work(candidate, reference)

    @staticmethod
    def _is_same_work(candidate: BookMetadata, reference: BookMetadata) -> bool:
        """Une autre édition du même ouvrage : titre proche ET même auteur.

        Sans cette double condition, une recherche par titre remonte des ouvrages voisins
        (« De la couleur des choses ») qui ne sont pas des éditions du livre demandé.
        """
        if title_similarity(candidate.title, reference.title) < SAME_WORK_TITLE_SIMILARITY_MIN:
            return False
        if candidate.author and reference.author:
            return same_author(candidate.author, reference.author)
        return normalize_text(candidate.title) == normalize_text(reference.title)

    @staticmethod
    def _attach_recommendations(meta: BookMetadata) -> BookMetadata:
        """Enrichit une notice avec les mots-clés recommandés (mutation en place)."""
        includes, excludes, variants = generate_recommended_keywords(meta.title, meta.author)
        meta.recommended_includes = includes
        meta.recommended_excludes = excludes
        meta.recommended_variants = variants
        return meta

    async def lookup_by_title_and_author(self, user_input: str) -> CatalogueVerdict:
        """Recherche par titre et indice d'auteur, avec le verdict qui l'accompagne.

        Le retour ne porte pas seulement la notice : il porte aussi **qui a parlé**.
        Sans cela, une BNF vide et une BNF muette se lisaient pareil, et une alerte
        de série ne pouvait pas dire « aucune source ne catalogue cet ouvrage » sans
        risquer de l'annoncer sur une panne réseau. Les décisions d'autorité sont
        inchangées : mêmes requêtes, même ordre, même repli.
        """
        title_cand, author_cand = parse_query_title_and_author(user_input)
        clean_title_q = clean_title_query(title_cand)

        attempts: list[ProviderAttempt] = []

        # Requête BNF avec le titre
        cql = f'bib.title all "{clean_title_q}"'
        result, _panne = await self._bnf_records_result(
            cql, author_hint=author_cand, title_hint=clean_title_q
        )
        attempts.append(_attempt(AUTORITE_BNF, "title", result))
        records = list(result.items)

        if not records and author_cand:
            # Essai plus large si pas de résultat
            cql_broad = f'bib.anywhere all "{clean_title_q}"'
            broad, _panne_broad = await self._bnf_records_result(
                cql_broad, author_hint=author_cand, title_hint=clean_title_q
            )
            attempts.append(_attempt(AUTORITE_BNF, "title_anywhere", broad))
            records = list(broad.items)

        if records:
            return _verdict(attempts, self._attach_recommendations(records[0]))

        fallback, ol_attempt = await self._resolve_via_openlibrary(clean_title_q, author_cand)
        attempts.append(ol_attempt)
        if fallback is not None:
            return _verdict(attempts, fallback.best)
        google, google_attempts = await self._resolve_via_google_books(clean_title_q, author_cand)
        attempts.extend(google_attempts)
        return _verdict(attempts, google.best if google else None)

    async def lookup_series_notices(
        self,
        series_title: str,
        author_hint: str | None = None,
        *,
        limit: int = MAX_SERIES_BOOKS,
        for_creation: bool = False,
        reference_publisher: str | None = None,
    ) -> CatalogueVerdict:
        """Notices distinctes d'une série — ses tomes — quand une autorité les nomme.

        Une série suivie par son nom (« Blacksad ») n'a aucune fiche : ses annonces
        partaient sans cote tant qu'aucun tome n'avait été lié. Chercher par titre
        remonte les tomes, dont le titre commence par celui de la série. Un titre
        n'est pas une identité (ADR-021) : sans concordance d'auteur, rien n'est
        rattaché, et la recherche reste bornée à `limit` notices.

        La BNF reste la source primaire ; quand elle rend **moins** de notices que
        `limit`, OpenLibrary complète les tomes manquants (ADR-033). Ce repli ne
        remplace jamais une notice BNF : il ne voit que les ISBN que la BNF n'a pas
        déjà donnés, et ses notices portent leur provenance (`AUTORITE_OPENLIBRARY`),
        donc `provenance_note` les distingue d'une confirmation BNF.

        Le verdict est celui de **cette** recherche, repli compris : une série sans
        aucun tome retenu, dont les autorités interrogées ont pourtant toutes
        répondu, n'est pas une recherche en cours — c'est une réponse, et elle se dit
        (ADR-041). Une autorité muette interdit la conclusion : son silence ne prouve
        rien.
        """
        wanted = normalize_text(series_title)
        if not wanted:
            # Titre vide : aucune autorité n'a été interrogée, et le verdict ne
            # pourra jamais dire « personne ne le connaît » — personne n'a cherché.
            return CatalogueVerdict()
        clean_title = clean_title_query(series_title)
        attempts: list[ProviderAttempt] = []
        result, _panne = await self._bnf_records_result(
            f'bib.title all "{clean_title}"',
            author_hint=author_hint,
            title_hint=clean_title,
            **({"series_pages": True} if for_creation else {}),
        )
        attempts.append(_attempt(AUTORITE_BNF, "title", result))
        records = list(result.items)
        if not records and author_hint:
            broad, _panne_broad = await self._bnf_records_result(
                f'bib.anywhere all "{clean_title}"',
                author_hint=author_hint,
                title_hint=clean_title,
                **({"series_pages": True} if for_creation else {}),
            )
            attempts.append(_attempt(AUTORITE_BNF, "title_anywhere", broad))
            records = list(broad.items)
            result = broad

        selected: list[BookMetadata] = []
        candidates: list[SeriesVolume] = []
        rejected: list[str] = []
        seen: set[str] = set()
        for record in records:
            if (not record.isbn13 and not for_creation) or (
                record.isbn13 and record.isbn13 in seen
            ):
                continue
            notice_title = normalize_text(record.title)
            is_series_member = notice_title.startswith(wanted) or (
                title_similarity(record.title, series_title) >= TITLE_SIMILARITY_MIN
            )
            if not is_series_member:
                continue
            if author_hint and record.author and not same_author(record.author, author_hint):
                continue
            if record.isbn13:
                seen.add(record.isbn13)
            selected.append(replace(record, title=record.volume_title or record.title))
            if len(selected) >= limit and not for_creation:
                break
        ol_limited = False
        if len(selected) < limit or for_creation:
            completed, ol_result = await self._complete_series_from_openlibrary(
                series_title=series_title,
                wanted=wanted,
                author_hint=author_hint,
                seen=seen,
                limit=MAX_SERIES_BOOKS if for_creation else limit - len(selected),
                include_unidentified=for_creation,
            )
            # Le repli nourrit le verdict comme la voie BNF : sans sa tentative, une
            # série vide se lirait « personne ne la connaît » alors qu'OpenLibrary
            # s'est peut-être tue — et un silence ne prouve rien.
            if ol_result is not None:
                ol_limited = ol_result.error_code == "series_limit"
                attempts.append(_attempt(AUTORITE_OPENLIBRARY, "title", ol_result))
            selected.extend(completed)
        if for_creation:
            google = self._google_books_client()
            if google.is_configured:
                google_result = await google.search_by_title_result(series_title, author_hint)
                attempts.append(_attempt(AUTORITE_GOOGLE_BOOKS, "title", google_result))
                for item in google_result.items:
                    meta = self._google_volume_notice(item)
                    if (
                        title_similarity(meta.title, series_title) >= TITLE_SIMILARITY_MIN
                        and (not author_hint or not meta.author
                             or same_author(meta.author, author_hint))
                    ):
                        selected.append(meta)
            for meta in selected:
                number = meta.volume_number or notice_volume(meta.volume_title or meta.title)
                if number is None:
                    continue
                code = isbn_from_identifier(meta.isbn13)
                outcomes: list[IsbnVerdict] = []
                verified = (
                    await self.verify_isbn(code, meta.volume_title or meta.title, outcomes=outcomes)
                    if code else None
                )
                if code and any(
                    verdict.outcome == "contradiction" and verdict.notice is not None
                    and verdict.notice.source == AUTORITE_BNF for verdict in outcomes
                ):
                    rejected.append(code)
                confirmed = (
                    code if verified is not None and verified.source == AUTORITE_BNF
                    and isbn_from_identifier(verified.isbn13) == code
                    and verified.volume_number == number
                    and (not author_hint or not verified.author
                         or same_author(verified.author, author_hint)) else None
                )
                if verified is not None and confirmed:
                    meta = replace(
                        meta, isbn13=confirmed, language=verified.language or meta.language,
                        author=verified.author or meta.author,
                        publisher=verified.publisher, publication_year=verified.publication_year,
                        format_detail=verified.format_detail,
                    )
                candidates.append(SeriesVolume(number, meta, confirmed))
            volumes = select_series_volumes(candidates, publisher=reference_publisher)
            counts = {meta.declared_volume_count for meta in selected
                      if meta.declared_volume_count is not None}
            total = next(iter(counts)) if len(counts) == 1 else None
            if total is not None and 0 < total <= MAX_SERIES_BOOKS:
                known = {v.number for v in volumes}
                volumes = select_series_volumes([
                    *volumes, *(SeriesVolume(n, BookMetadata(title=series_title, source=""))
                                for n in range(1, total + 1) if n not in known),
                ])
            verdict = _verdict(attempts, *(v.notice for v in volumes if v.isbn13))
            return replace(verdict, volumes=volumes, truncated=(
                result.error_code == "series_limit" or ol_limited or len(volumes) > limit
                or (total is not None and total > limit)
            ), rejected_isbns=tuple(dict.fromkeys(rejected)),
                declared_volume_count=total, inconsistent_counts=(
                len(counts) > 1 or (total is not None and any(v.number > total for v in volumes))
            ))
        return _verdict(attempts, *selected)

    async def _complete_series_from_openlibrary(
        self,
        *,
        series_title: str,
        wanted: str,
        author_hint: str | None,
        seen: set[str],
        limit: int,
        include_unidentified: bool = False,
    ) -> tuple[list[BookMetadata], ProviderQueryResult[dict[str, Any]] | None]:
        """Complète une série par OpenLibrary — seulement les tomes que la BNF a tus.

        Repli d'enrichissement, jamais de substitution : `seen` porte les ISBN déjà
        retenus par la BNF, et un document qui les recoupe est ignoré. Les critères
        d'appartenance sont ceux de la voie BNF — préfixe de titre normalisé ou
        similarité suffisante, cohérence d'auteur quand elle est connue — pour que
        les deux sources ne se contredisent pas sur ce qu'est un tome de la série.
        Les notices rendues gardent leur provenance OpenLibrary (`BookMetadata.source`).

        La requête réellement partie est rendue avec ses notices : c'est elle qui fait
        entrer OpenLibrary dans le verdict (`None` quand rien n'a été demandé).
        """
        if limit <= 0:
            return [], None
        params: dict[str, str] = {"title": clean_title_query(series_title)}
        if author_hint:
            params["author"] = author_hint
        works, result = await self._openlibrary_works(
            params, **({"include_unidentified": True} if include_unidentified else {})
        )

        found: list[BookMetadata] = []
        metas: list[BookMetadata] = []
        for work in works:
            codes = work.edition_isbns if include_unidentified else []
            metas.extend(replace(work.meta, isbn13=isbn_from_identifier(code))
                         for code in codes)
            if not codes:
                metas.append(work.meta)
        if include_unidentified and len(metas) > 100:
            result = replace(result, error_code="series_limit")
            metas = metas[:100]
        for meta in metas:
            if (not meta.isbn13 and not include_unidentified) or (
                meta.isbn13 and meta.isbn13 in seen
            ):
                continue
            notice_title = normalize_text(meta.title)
            is_series_member = notice_title.startswith(wanted) or (
                title_similarity(meta.title, series_title) >= TITLE_SIMILARITY_MIN
            )
            if not is_series_member:
                continue
            if author_hint and meta.author and not same_author(meta.author, author_hint):
                continue
            if meta.isbn13:
                seen.add(meta.isbn13)
            found.append(await self._openlibrary_series_volume(meta))
            if len(found) >= limit and not include_unidentified:
                break
        return found, result

    async def _openlibrary_series_volume(self, meta: BookMetadata) -> BookMetadata:
        """Lit le numéro sur l'édition exacte, jamais sur la position dans une série.

        La recherche OpenLibrary renvoie le titre de l'œuvre sans son numéro ;
        l'édition du même ISBN porte parfois « Vol. 1 ». Une panne conserve la
        notice initiale, et un ISBN différent ne peut jamais la réécrire.
        """
        if not meta.isbn13 or extract_tome_number_from_title(meta.title) is not None:
            return meta
        return await self._openlibrary_edition_metadata(meta)

    async def _openlibrary_edition_metadata(
        self, meta: BookMetadata, *, require_volume: bool = False
    ) -> BookMetadata:
        """Titre, numéro et langue de l'édition exacte ; les langues d'œuvre sont ignorées."""
        fallback = replace(meta, language=None)
        if require_volume:
            # Un numéro sur l'œuvre agrégée ne confirme pas l'édition de cet ISBN.
            fallback.volume_number = None
        if not meta.isbn13:
            return fallback
        try:
            client = self.http_client or get_shared_http_client()
            response = await client.get(
                f"https://openlibrary.org/isbn/{meta.isbn13}.json",
                follow_redirects=True,
                timeout=self.timeout_seconds,
            )
            if response.status_code != 200:
                return fallback
            payload = response.json()
            if not isinstance(payload, dict) or meta.isbn13 not in payload.get("isbn_13", []):
                return fallback
            title = payload.get("title")
            subtitle = payload.get("subtitle")
            if isinstance(title, str) and isinstance(subtitle, str):
                title = f"{title} : {subtitle}"
            if (
                not isinstance(title, str)
                or title_similarity(title, meta.title) < TITLE_SIMILARITY_MIN
            ):
                return fallback
            languages = payload.get("languages")
            language = None
            if isinstance(languages, list) and len(languages) == 1:
                entry = languages[0]
                if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                    language = normalize_edition_language(entry["key"])
            number = notice_volume(title)
            edition = replace(
                meta, title=title if number is not None else meta.title,
                volume_title=title if number is not None else meta.volume_title,
                volume_number=number, language=language,
            )
            if require_volume:
                edition.volume_number = number
            return edition
        except Exception as exc:
            logger.debug("Notice d'édition OpenLibrary indisponible : %s", type(exc).__name__)
        return fallback

    async def _bnf_records_result(
        self,
        cql_query: str,
        author_hint: str | None = None,
        title_hint: str | None = None,
        *, series_pages: bool = False, start_record: int = 1,
    ) -> tuple[ProviderQueryResult[BookMetadata], str | None]:
        """Notices SRU typées, et le motif de panne si l'autorité n'a pas répondu.

        Le second élément est ce qui manque à une liste vide pour être une réponse :
        sans lui, `[]` dit aussi bien « la BNF ignore cet ISBN » que « la requête a
        échoué », et l'appelant qui doit rendre compte ne peut pas trancher. C'est
        cette distinction qui fait un verdict (`is_catalogue_exhausted`, ADR-041).

        Les libellés de panne reprennent les expressions historiques à l'identique
        — le contrat neuf (`error_code`) ne porte, lui, que des codes contrôlés,
        jamais de message d'exception. Les logs ne portent que des noms de classes :
        l'URL SRU contient la saisie utilisateur, qui n'a rien à y faire.
        """
        params = {
            "version": "1.2",
            "operation": "searchRetrieve",
            "query": cql_query,
            "recordSchema": "dublincore",
            "maximumRecords": "10",
        }
        if start_record > 1:
            params["startRecord"] = str(start_record)
        url = f"{self.BNF_SRU_URL}?{urllib.parse.urlencode(params)}"

        try:
            client = self.http_client or get_shared_http_client()
            resp = await client.get(
                url,
                timeout=self.timeout_seconds,
                headers={"User-Agent": "BotRadar/1.0"},
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            logger.warning("BNF SRU : délai dépassé (%s)", type(exc).__name__)
            return (
                ProviderQueryResult(
                    status=ProviderQueryStatus.TIMEOUT, error_code="timeout"
                ),
                f"{type(exc).__name__}: {exc}",
            )
        except Exception as exc:
            logger.warning("BNF SRU : requête en échec (%s)", type(exc).__name__)
            return (
                ProviderQueryResult(
                    status=ProviderQueryStatus.UNAVAILABLE,
                    error_code=type(exc).__name__,
                ),
                f"{type(exc).__name__}: {exc}",
            )

        if resp.status_code in (401, 403):
            logger.warning("BNF SRU : authentification refusée (%d)", resp.status_code)
            return (
                ProviderQueryResult(
                    status=ProviderQueryStatus.UNAUTHORIZED,
                    error_code=f"http_{resp.status_code}",
                ),
                f"statut HTTP {resp.status_code}",
            )
        if resp.status_code == 429:
            logger.warning("BNF SRU : quota dépassé (429)")
            return (
                ProviderQueryResult(
                    status=ProviderQueryStatus.RATE_LIMITED, error_code="http_429"
                ),
                "statut HTTP 429",
            )
        if resp.status_code != 200:
            logger.warning("BNF SRU returned status %d", resp.status_code)
            return (
                ProviderQueryResult(
                    status=ProviderQueryStatus.UNAVAILABLE,
                    error_code=f"http_{resp.status_code}",
                ),
                f"statut HTTP {resp.status_code}",
            )

        # Le `XMLParser` de la C de CPython ne prend que `target` et `encoding` :
        # `resolve_entities` comme `forbid_entities` y lèvent un TypeError, qui
        # transformait chaque réponse de la BNF en « autorité muette » — 11 tests
        # rouges et `mypy app` en erreur. Ce parseur refuse déjà les entités
        # externes (`undefined entity`) ; ce qui reste possible est la déclaration
        # d'entités internes, alors un document qui en déclare une n'est pas lu.
        try:
            if _declares_doctype(resp.content):
                logger.warning("Réponse SRU BNF écartée : elle déclare une DOCTYPE")
                return (
                    ProviderQueryResult(
                        status=ProviderQueryStatus.INVALID_RESPONSE,
                        error_code="doctype_declared",
                    ),
                    "DOCTYPE déclaré dans la réponse",
                )
            root = ET.fromstring(resp.content)
            raw_records = root.findall(".//{http://www.loc.gov/zing/srw/}recordData")
            has_results = len(raw_records) > 0
            results: list[tuple[float, BookMetadata]] = []
            for record in raw_records:
                titles = [t.text for t in record.findall(
                    ".//{http://purl.org/dc/elements/1.1/}title") if t.text]
                creators = [c.text for c in record.findall(
                    ".//{http://purl.org/dc/elements/1.1/}creator") if c.text]
                identifiers = [i.text for i in record.findall(
                    ".//{http://purl.org/dc/elements/1.1/}identifier") if i.text]
                publishers = [p.text for p in record.findall(
                    ".//{http://purl.org/dc/elements/1.1/}publisher") if p.text]
                dates = [d.text for d in record.findall(
                    ".//{http://purl.org/dc/elements/1.1/}date") if d.text]
                languages = [lang.text for lang in record.findall(
                    ".//{http://purl.org/dc/elements/1.1/}language") if lang.text]
                formats = [f.text for f in record.findall(
                    ".//{http://purl.org/dc/elements/1.1/}format") if f.text]

                if not titles:
                    continue

                # Une notice sans identifiant exploitable est écartée : mieux vaut
                # rien rendre qu'un rattachement douteux. Ce qui compte est que
                # l'identifiant soit *lu*, pas qu'il soit libellé « ISBN » : une
                # notice ne portant qu'un EAN-13 est aussi une notice de livre.
                isbn13 = next(
                    (found for found in map(isbn_from_identifier, identifiers) if found),
                    None,
                )
                if not isbn13 and not series_pages:
                    continue

                raw_identity = re.split(r"\s*/\s*", titles[0], maxsplit=1)[0]
                main_title = clean_bnf_title(titles[0])
                series_title = clean_bnf_series_title(titles[0])
                # Le titre de secours n'est conservé que s'il apprend quelque chose : un
                # marqueur de tome que le titre de base n'a plus.
                volume_title = series_title if series_title != main_title else None
                # Le sous-titre complet peut nommer un lot ou porter le numéro au milieu.
                # Le titre épuré seul ne doit ni fabriquer ni effacer cette preuve.
                if is_collection(raw_identity):
                    volume_title = raw_identity
                number = notice_volume(raw_identity)
                author_name = clean_bnf_creator(creators[0]) if creators else None
                pub_name = clean_bnf_publisher(publishers[0]) if publishers else None
                publication_year = _parse_year(dates[0]) if dates else None
                format_detail = formats[0].strip() if formats else None

                # Similarité du titre : critère dominant, une notice portant sur un autre
                # ouvrage ne doit jamais gagner sur la bonne œuvre demandée.
                score = 1.0
                if title_hint:
                    score += 3.0 * title_similarity(main_title, title_hint)

                # Calcul d'un score de similarité d'auteur si un indice d'auteur est donné
                if author_hint and creators:
                    hint_norm = normalize_text(author_hint)
                    creators_norm = " ".join(normalize_text(c) for c in creators)
                    ratio = difflib.SequenceMatcher(None, hint_norm, creators_norm).ratio()
                    hint_words = [w for w in hint_norm.split() if len(w) >= 3]
                    for word in hint_words:
                        if word in creators_norm:
                            ratio += 0.4
                    score += ratio
                    # Un indice d'auteur totalement absent de la notice déclasse le résultat :
                    # plusieurs ouvrages portent le même titre, pas le même auteur.
                    if hint_words and not any(word in creators_norm for word in hint_words):
                        score -= 0.5

                # Bonus pour les éditions francophones (BD et éditeurs de référence)
                if isbn13 and (isbn13.startswith("9782") or isbn13.startswith("2")):
                    score += 0.5
                if pub_name:
                    norm_pub = normalize_text(pub_name)
                    fr_publishers = {
                        "delcourt", "dupuis", "dargaud", "glenat", "casterman",
                        "lombard", "futuropolis", "soleil", "panini", "urban",
                        "ankama", "paquet", "bamboo", "actes sud",
                    }
                    if any(fp in norm_pub for fp in fr_publishers):
                        score += 0.6

                # Une notice datée est une fiche complète ; une notice sans date est
                # souvent un doublon d'édition : elle sert de repli, pas de référence.
                if publication_year:
                    score += 0.3

                meta = BookMetadata(
                    title=main_title,
                    author=author_name,
                    publisher=pub_name,
                    isbn13=isbn13,
                    source=AUTORITE_BNF,
                    publication_year=publication_year,
                    format_detail=format_detail,
                    volume_title=volume_title,
                    volume_number=number,
                    declared_volume_count=_declared_series_count(raw_identity),
                    language=(
                        normalize_edition_language(languages[0]) if len(languages) == 1 else None
                    ),
                )
                results.append((score, meta))
        except ET.ParseError as exc:
            logger.warning("BNF SRU : XML illisible (%s)", type(exc).__name__)
            return (
                ProviderQueryResult(
                    status=ProviderQueryStatus.INVALID_RESPONSE,
                    error_code="invalid_xml",
                ),
                f"{type(exc).__name__}: {exc}",
            )
        except Exception as exc:
            logger.warning("BNF SRU : réponse inexploitable (%s)", type(exc).__name__)
            return (
                ProviderQueryResult(
                    status=ProviderQueryStatus.UNAVAILABLE,
                    error_code=type(exc).__name__,
                    has_results=has_results,
                ),
                f"{type(exc).__name__}: {exc}",
            )

        results.sort(key=lambda x: x[0], reverse=True)
        if series_pages:
            count_text = root.findtext("{http://www.loc.gov/zing/srw/}numberOfRecords")
            count = int(count_text) if count_text and count_text.isdigit() else len(raw_records)
            next_start = start_record + len(raw_records)
            if raw_records and next_start <= count and next_start <= 100:
                following, panne = await self._bnf_records_result(
                    cql_query, author_hint, title_hint,
                    series_pages=True, start_record=next_start,
                )
                return replace(
                    following, items=tuple(meta for _, meta in results) + following.items,
                    has_results=has_results or following.has_results,
                ), panne
            if next_start <= count:
                return ProviderQueryResult(
                    items=tuple(meta for _, meta in results), has_results=has_results,
                    error_code="series_limit",
                ), None
        notices = [item[1] for item in results]
        return (
            ProviderQueryResult(
                status=ProviderQueryStatus.SUCCESS,
                items=tuple(notices),
                has_results=has_results,
            ),
            None,
        )

    async def _resolve_via_openlibrary(
        self, title: str, author_hint: str | None = None
    ) -> tuple[BookResolution | None, ProviderAttempt]:
        """Résolution de repli : OpenLibrary ne répond que si la BNF n'a rien donné.

        La confiance est moindre (`MEDIUM`) et la provenance est remontée jusqu'au
        récap Telegram : l'utilisateur doit savoir que l'ISBN vient d'une autre source.
        La tentative décrit la requête réellement partie, même en échec.
        """
        params: dict[str, str] = {"title": title}
        if author_hint:
            params["author"] = author_hint

        works, docs_result = await self._openlibrary_works(params)
        attempt = _attempt(AUTORITE_OPENLIBRARY, "title", docs_result)
        if requested_volume(title) is not None:
            for work in works:
                work.meta = await self._openlibrary_edition_metadata(work.meta, require_volume=True)
        candidates = [
            work for work in works
            if title_similarity(work.meta.title, title) >= TITLE_SIMILARITY_MIN
            and matches_requested_volume(work.meta, title)
        ]
        if not candidates:
            return None, attempt

        best = candidates[0].meta
        if requested_volume(title) is None:
            best = await self._openlibrary_edition_metadata(best)
        alternatives = [
            replace(best, isbn13=isbn, language=None)
            for isbn in candidates[0].edition_isbns
            if requested_volume(title) is None
            and isbn != best.isbn13 and isbn.startswith(FRENCH_ISBN_PREFIXES)
        ][:MAX_ALTERNATIVE_EDITIONS]
        # Chaque document OpenLibrary est une œuvre : deux documents de même titre et
        # d'auteurs différents sont des homonymes, pas des éditions.
        other_works = [
            work.meta
            for work in candidates[1 : 1 + MAX_ALTERNATIVE_EDITIONS]
            if self._is_homonym(work.meta, best, title)
        ]
        return BookResolution(
            best=best,
            alternatives=alternatives,
            other_works=other_works,
            author_hint_match=self._author_hint_match(best, author_hint),
            notice_titles=tuple(
                dict.fromkeys(work.meta.title for work in candidates)
            ),
        ), attempt

    def _google_books_client(self) -> GoogleBooksClient:
        """Le client de la troisième autorité, construit depuis la configuration.

        Sans clé, `is_configured` est faux : la source n'est pas interrogée, et elle
        n'est donc jamais annoncée comme consultée. La clé vient de la configuration
        et pas d'un paramètre, parce que le service est le seul à savoir quelles
        autorités il interroge.
        """
        return GoogleBooksClient(
            api_key=settings.GOOGLE_BOOKS_API_KEY.get_secret_value(),
            # Le client du service, pas un second : les tests injectent le leur, et un
            # service qui se servirait du client partagé ignorerait l'injection.
            http_client=self.http_client,
        )

    async def _resolve_via_google_books(
        self, title: str, author_hint: str | None = None
    ) -> tuple[BookResolution | None, tuple[ProviderAttempt, ...]]:
        """Dernier repli d'une résolution par titre, après la BNF et OpenLibrary.

        Même contrat que `_resolve_via_openlibrary` : confiance `MEDIUM`, provenance
        remontée jusqu'au récap, et aucune notice retenue si le titre ne correspond pas.
        Quand la recherche ciblée (`intitle:`) réussit sans rien rendre, une
        recherche libre (titre + auteur, sans opérateur) fournit des candidats
        supplémentaires — soumis aux mêmes filtres, jamais confirmés d'office.
        Toute autre issue (panne, quota, refus) vaut absence de candidats, comme
        avant : les décisions globales ne changent pas dans ce lot. Chaque requête
        réellement partie laisse sa tentative, le repli libre seulement s'il part.
        """
        client = self._google_books_client()
        if not client.is_configured:
            return None, ()

        attempts: list[ProviderAttempt] = []
        targeted = await client.search_by_title_result(title, author_hint)
        attempts.append(_attempt(AUTORITE_GOOGLE_BOOKS, "title", targeted))
        volumes = list(targeted.items)
        if targeted.status == ProviderQueryStatus.SUCCESS and not volumes:
            fallback = await client.search_free_result(title, author_hint)
            attempts.append(_attempt(AUTORITE_GOOGLE_BOOKS, "free", fallback))
            if fallback.status == ProviderQueryStatus.SUCCESS:
                volumes = list(fallback.items)
        candidates = [
            self._attach_recommendations(self._google_volume_notice(volume))
            for volume in volumes
        ]
        candidates = [
            meta
            for meta in candidates
            if title_similarity(meta.title, title) >= TITLE_SIMILARITY_MIN
            and matches_requested_volume(meta, title)
        ]
        if not candidates:
            return None, tuple(attempts)

        # Google classe par pertinence, pas par titre : c'est la similarité qui décide
        # ici, comme partout ailleurs, pour qu'une notice voisine ne passe pas devant
        # l'œuvre demandée.
        candidates.sort(key=lambda meta: title_similarity(meta.title, title), reverse=True)
        candidates = self._prefer_author_hint(candidates, author_hint)
        best = candidates[0]
        alternatives = [
            candidate
            for candidate in candidates[1 : 1 + MAX_ALTERNATIVE_EDITIONS]
            if self._is_same_work(candidate, best)
        ]
        other_works = [
            candidate
            for candidate in candidates[1:]
            if self._is_homonym(candidate, best, title)
        ][:MAX_ALTERNATIVE_EDITIONS]
        return BookResolution(
            best=best,
            alternatives=alternatives,
            other_works=other_works,
            author_hint_match=self._author_hint_match(best, author_hint),
            notice_titles=tuple(
                dict.fromkeys(meta.volume_title or meta.title for meta in candidates)
            ),
        ), tuple(attempts)

    async def _google_books_notice_by_isbn(
        self, client: GoogleBooksClient, isbn: str
    ) -> BookMetadata | None:
        """Notice attestant **réellement** ce code, ou rien.

        Une requête `isbn:` ne garantit pas que la notice rendue porte le code demandé
        (une œuvre agrège plusieurs éditions) : un repli ne confirme que ce qu'il lit,
        et un code absent de la notice rendrait une confirmation qu'aucune donnée
        n'appuie. C'est la même règle que pour la BNF et OpenLibrary. Pas de
        repli libre ici : un ISBN seul ne fait pas une requête libre fiable.
        """
        notice, _result = await self._google_books_notice_result(client, isbn)
        return notice

    async def _google_books_notice_result(
        self, client: GoogleBooksClient, isbn: str
    ) -> tuple[BookMetadata | None, ProviderQueryResult[GoogleVolume]]:
        """Notice attestant ce code, et le résultat typé de la requête partie."""
        result = await client.search_by_isbn_result(isbn)
        for volume in result.items:
            if isbn in volume.isbns:
                return (
                    self._attach_recommendations(self._google_volume_notice(volume)),
                    result,
                )
        return None, result

    @staticmethod
    def _google_volume_notice(volume: GoogleVolume) -> BookMetadata:
        """Une notice Google Books, et sa provenance déclarée."""
        return BookMetadata(
            title=volume.title,
            volume_title=f"{volume.title} {volume.subtitle}" if volume.subtitle else None,
            declared_volume_count=_declared_series_count(
                f"{volume.title} {volume.subtitle or ''}"
            ),
            author=volume.author,
            publisher=volume.publisher,
            isbn13=volume.isbn13,
            source=AUTORITE_GOOGLE_BOOKS,
            confidence=MatchConfidence.MEDIUM,
            publication_year=volume.publication_year,
            format_detail=f"{volume.page_count} p." if volume.page_count else None,
            language=normalize_edition_language(volume.language),
        )

    async def _openlibrary_works(
        self, params: dict[str, str], *, include_unidentified: bool = False,
    ) -> tuple[list[_OpenLibraryWork], ProviderQueryResult[dict[str, Any]]]:
        """Notices OpenLibrary triées par pertinence, et le résultat typé brut."""
        result = await self._query_openlibrary_result(params)
        docs = result.items
        title_hint = params.get("title")
        author_hint = params.get("author")
        expected_isbn = normalize_isbn(params.get("isbn"))

        scored: list[tuple[float, _OpenLibraryWork]] = []
        for doc in docs:
            work = self._build_openlibrary_work(
                doc, expected_isbn, include_unidentified=include_unidentified,
            )
            if not work:
                continue
            score = 1.0
            if title_hint:
                score += 3.0 * title_similarity(work.meta.title, title_hint)
            if author_hint and work.meta.author:
                if same_author(work.meta.author, author_hint):
                    score += 1.0
                else:
                    # Un indice d'auteur contredit par la notice doit déclasser le résultat.
                    score -= 0.5
            scored.append((score, work))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [work for _score, work in scored], result

    async def _query_openlibrary(self, params: dict[str, str]) -> list[dict[str, Any]]:
        """Interroge l'API Search d'OpenLibrary (aucune clé, aucune dépendance)."""
        return list((await self._query_openlibrary_result(params)).items)

    async def _query_openlibrary_result(
        self, params: dict[str, str]
    ) -> ProviderQueryResult[dict[str, Any]]:
        """Documents OpenLibrary typés : statut distingué d'une liste vide.

        Même requête et même lecture qu'avant : seuls s'ajoutent le statut et
        `has_results` (documents bruts reçus, avant construction des notices).
        Les logs ne portent que des noms de classes, comme côté Google.
        """
        query = {
            **params,
            "fields": self.OPENLIBRARY_FIELDS,
            "limit": str(self.OPENLIBRARY_MAX_RESULTS),
        }
        url = f"{self.OPENLIBRARY_SEARCH_URL}?{urllib.parse.urlencode(query)}"
        try:
            client = self.http_client or get_shared_http_client()
            resp = await client.get(
                url,
                timeout=self.timeout_seconds,
                headers={"User-Agent": "BotRadar/1.0"},
            )
        except (httpx.TimeoutException, TimeoutError):
            logger.warning("OpenLibrary : délai dépassé")
            return ProviderQueryResult(
                status=ProviderQueryStatus.TIMEOUT, error_code="timeout"
            )
        except Exception as exc:
            logger.warning("OpenLibrary : requête en échec (%s)", type(exc).__name__)
            return ProviderQueryResult(
                status=ProviderQueryStatus.UNAVAILABLE,
                error_code=type(exc).__name__,
            )
        if resp.status_code in (401, 403):
            logger.warning("OpenLibrary : authentification refusée (%d)", resp.status_code)
            return ProviderQueryResult(
                status=ProviderQueryStatus.UNAUTHORIZED,
                error_code=f"http_{resp.status_code}",
            )
        if resp.status_code == 429:
            logger.warning("OpenLibrary : quota dépassé (429)")
            return ProviderQueryResult(
                status=ProviderQueryStatus.RATE_LIMITED, error_code="http_429"
            )
        if resp.status_code != 200:
            logger.warning("OpenLibrary a renvoyé le statut %d", resp.status_code)
            return ProviderQueryResult(
                status=ProviderQueryStatus.UNAVAILABLE,
                error_code=f"http_{resp.status_code}",
            )
        try:
            payload = resp.json()
        except Exception:
            logger.warning("OpenLibrary : charge utile JSON illisible")
            return ProviderQueryResult(
                status=ProviderQueryStatus.INVALID_RESPONSE, error_code="invalid_json"
            )

        docs = payload.get("docs") if isinstance(payload, dict) else None
        if not isinstance(docs, list):
            if isinstance(payload, dict) and payload.get("numFound", 0) == 0:
                return ProviderQueryResult(status=ProviderQueryStatus.SUCCESS)
            logger.warning("OpenLibrary : structure de réponse inattendue")
            return ProviderQueryResult(
                status=ProviderQueryStatus.INVALID_RESPONSE,
                error_code="unexpected_structure",
            )
        items = tuple(doc for doc in docs if isinstance(doc, dict))
        return ProviderQueryResult(
            status=ProviderQueryStatus.SUCCESS,
            items=items,
            has_results=len(docs) > 0,
        )

    def _build_openlibrary_work(
        self, doc: dict[str, Any], expected_isbn: str | None,
        *, include_unidentified: bool = False,
    ) -> _OpenLibraryWork | None:
        """Convertit un document OpenLibrary en notice avec l'ISBN pertinent retenu."""
        title = str(doc.get("title") or "").strip()
        if not title:
            return None

        edition_isbns = self._valid_openlibrary_isbns(doc.get("isbn"))
        isbn13 = self._select_openlibrary_isbn(edition_isbns, expected_isbn)
        if not isbn13 and not include_unidentified:
            return None

        authors = doc.get("author_name") or []
        publishers = doc.get("publisher") or []
        pages = doc.get("number_of_pages_median")
        year = doc.get("first_publish_year")
        meta = BookMetadata(
            title=title,
            author=clean_openlibrary_author(str(authors[0])) if authors else None,
            publisher=str(publishers[0]).strip() if publishers else None,
            isbn13=isbn13,
            source=AUTORITE_OPENLIBRARY,
            confidence=MatchConfidence.MEDIUM,
            publication_year=year if isinstance(year, int) else None,
            format_detail=f"{pages} p." if isinstance(pages, int) else None,
            declared_volume_count=_declared_series_count(title),
        )
        return _OpenLibraryWork(meta=meta, edition_isbns=edition_isbns)

    @staticmethod
    def _valid_openlibrary_isbns(raw_isbns: Any) -> list[str]:
        """Normalise la liste d'ISBN d'un document en codes réellement exploitables."""
        if not isinstance(raw_isbns, list):
            return []
        cleaned: list[str] = []
        for raw in raw_isbns:
            code = normalize_isbn(str(raw))
            if code and is_valid_isbn(code) and code not in cleaned:
                cleaned.append(code)
        return cleaned

    @staticmethod
    def _select_openlibrary_isbn(
        edition_isbns: list[str], expected_isbn: str | None
    ) -> str | None:
        """Choisit l'ISBN pertinent parmi les éditions regroupées par OpenLibrary.

        OpenLibrary agrège toutes les éditions d'une œuvre dans un seul document : la
        liste contient donc l'édition d'origine mais aussi ses traductions. On retient
        le code demandé, puis un ISBN-13, puis une édition francophone.
        """
        codes13 = [isbn for isbn in edition_isbns if len(isbn) == 13]
        pool = codes13 or edition_isbns
        if not pool:
            return None

        if expected_isbn:
            for isbn in pool:
                if isbn == expected_isbn or isbn10_to_isbn13(isbn) == expected_isbn:
                    return isbn
        for isbn in pool:
            if isbn.startswith(FRENCH_ISBN_PREFIXES):
                return isbn
        return pool[0]

    async def _lookup_from_vinted_url(self, url: str) -> BookMetadata | None:
        """Extrait le texte de recherche ou l'ISBN d'une URL Vinted et effectue le lookup."""
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        search_text = params.get("search_text", [""])[0].strip()
        if search_text:
            return await self.lookup(search_text)
        return None
