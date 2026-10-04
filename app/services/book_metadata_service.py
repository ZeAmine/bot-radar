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
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from app.core.constants import MatchConfidence
from app.core.http_client import get_shared_http_client
from app.utils.isbn import is_valid_isbn, isbn10_to_isbn13, normalize_isbn
from app.utils.text import normalize_text

logger = logging.getLogger("radar.book_metadata")

STANDARD_BD_EXCLUDES = ["roman", "poche", "anglais", "english", "dvd"]

# Similarité minimale entre le titre demandé et la notice BNF pour valider une correspondance.
TITLE_SIMILARITY_MIN = 0.75
# Similarité minimale entre deux titres pour les considérer comme deux éditions de la même œuvre.
SAME_WORK_TITLE_SIMILARITY_MIN = 0.9
# Nombre maximum d'éditions alternatives remontées à l'utilisateur.
MAX_ALTERNATIVE_EDITIONS = 3

# Préfixes ISBN des éditions francophones (règle GF pour la langue française).
FRENCH_ISBN_PREFIXES = ("9782", "979")

# Identifiants des autorités, tels que `BookMetadata.source` les déclare. Les possède
# le service qui les interroge : un appelant qui les recopierait finirait par annoncer
# « ni la BNF ni OpenLibrary » le jour où un troisième repli entre en scène.
AUTORITE_BNF = "bnf"
AUTORITE_OPENLIBRARY = "openlibrary"

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


class EditionAlternative(BaseModel):
    """Édition d'une même œuvre, proposée au choix de l'utilisateur avant création."""

    isbn13: str
    publisher: str | None = None
    publication_year: int | None = None
    format_detail: str | None = None


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
    """

    best: BookMetadata
    alternatives: list[BookMetadata] = field(default_factory=list)
    other_works: list[BookMetadata] = field(default_factory=list)
    author_hint_match: AuthorHintMatch = "none"

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


OPENLIBRARY_SOURCE_NOTE = "Source OpenLibrary (repli BNF)"


def provenance_note(meta: BookMetadata) -> str | None:
    """Signale au récap une donnée d'identité issue d'une autorité secondaire."""
    return OPENLIBRARY_SOURCE_NOTE if meta.source == AUTORITE_OPENLIBRARY else None


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
            f"La BNF connaît « {found.title} » de {found_author}, "
            "pas l'ouvrage que tu as demandé"
        )
    others = len(resolution.other_works)
    if others == 1:
        return (
            f"« {found.title} » est aussi le titre d'une autre œuvre : "
            "surveillance par titre jusqu'au choix ci-dessous"
        )
    return (
        f"« {found.title} » est le titre de {others} autres œuvres : "
        "surveillance par titre jusqu'au choix ci-dessous"
    )


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


def clean_title_query(title: str) -> str:
    """Normalise un titre saisi pour la requête CQL de la BNF."""
    cleaned = re.sub(_TITLE_QUERY_ALLOWED_CHARS, " ", title).strip()
    return cleaned or title


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

    # Découpage par "de" (attention aux titres contenant "de", ex: "La saga de Grimr")
    parts_de = re.split(r"\s+de\s+", text, flags=re.IGNORECASE)
    if len(parts_de) > 1:
        last_part = parts_de[-1].strip()
        # Si le dernier segment ressemble à un nom d'auteur (1 à 4 mots max)
        if 1 <= len(last_part.split()) <= 4:
            author_candidate = _clean_author_candidate(last_part)
            title_candidate = " de ".join(parts_de[:-1]).strip()
            if title_candidate and author_candidate:
                return title_candidate, author_candidate

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


def clean_bnf_creator(raw_creator: str) -> str:
    """Formate le créateur BNF ('Nom, Prénom (dates). Fonction' -> 'Prénom Nom')."""
    m = re.match(r"^([^,]+),\s*([^(]+)", raw_creator)
    if m:
        last = m.group(1).strip()
        first = m.group(2).strip()
        return f"{first} {last}"
    return re.sub(r"\(.*?\).*|\..*", "", raw_creator).strip()


def clean_bnf_publisher(raw_publisher: str) -> str:
    """Nettoie l'éditeur BNF (ex: 'Éditions Delcourt (Paris)' -> 'Éditions Delcourt')."""
    return re.sub(r"\(.*?\)", "", raw_publisher).strip()


def generate_recommended_keywords(
    title: str, _author: str | None = None
) -> tuple[list[str], list[str], list[str]]:
    """Génère des mots-clés d'inclusion, d'exclusion et de variantes adaptés à l'œuvre."""
    norm_title = normalize_text(title)
    includes: list[str] = []

    if norm_title:
        includes.append(norm_title)

        # Si le titre commence par un article français, ajouter la version sans article
        for article in ["la ", "le ", "les ", "l "]:
            if norm_title.startswith(article):
                without_article = norm_title[len(article):].strip()
                if without_article and without_article not in includes:
                    includes.append(without_article)
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

        # 3. Recherche par Titre (+ Auteur éventuel)
        return await self.lookup_by_title_and_author(clean_q)

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
        clean_isbn = normalize_isbn(isbn)
        if not clean_isbn or not is_valid_isbn(clean_isbn):
            # Un code mal formé n'a été soumis à personne : le dire « inconnu »
            # accuserait une autorité de l'ignorer.
            return IsbnVerdict(outcome="inconnu")

        records, panne = await self._bnf_records(f'bib.isbn adj "{clean_isbn}"')
        if records:
            return IsbnVerdict(
                outcome="confirme",
                notice=self._attach_recommendations(records[0]),
                consulted=(AUTORITE_BNF,),
            )

        consulted = (AUTORITE_BNF, AUTORITE_OPENLIBRARY)
        fallback = await self._openlibrary_works({"isbn": clean_isbn})
        if fallback:
            return IsbnVerdict(
                outcome="silence" if panne else "confirme",
                notice=fallback[0].meta,
                consulted=consulted,
                panne=panne,
            )
        # OpenLibrary n'a rien donné non plus : sans `panne`, ce serait un verdict.
        return IsbnVerdict(
            outcome="silence" if panne else "inconnu",
            consulted=consulted,
            panne=panne,
        )

    async def verify_isbn(self, isbn13: str, expected_title: str) -> BookMetadata | None:
        """Confirme qu'un ISBN désigne bien l'œuvre attendue.

        Filet anti-hallucination : un ISBN bien formé mais attribué à une autre œuvre
        (préfixe d'éditeur recombiné par un LLM, par exemple) est rejeté sur le titre.
        Une notice obtenue pendant une panne ne confirme rien.
        """
        verdict = await self.confirm_isbn_for_title(isbn13, expected_title)
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

        if title_similarity(verdict.notice.title, expected_title) < TITLE_SIMILARITY_MIN:
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
        if not title.strip():
            return None

        clean_title_q = clean_title_query(title)
        records = await self._query_bnf_sru(
            f'bib.title all "{clean_title_q}"',
            author_hint=author_hint,
            title_hint=clean_title_q,
        )
        if not records and author_hint:
            records = await self._query_bnf_sru(
                f'bib.anywhere all "{clean_title_q}"',
                author_hint=author_hint,
                title_hint=clean_title_q,
            )

        candidates = [
            record
            for record in records
            if title_similarity(record.title, title) >= TITLE_SIMILARITY_MIN
        ]
        if not candidates:
            return await self._resolve_via_openlibrary(title, author_hint)

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
        return BookResolution(
            best=best,
            alternatives=alternatives,
            other_works=other_works,
            author_hint_match=self._author_hint_match(best, author_hint),
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
        if (
            normalize_text(candidate.title) != wanted
            or normalize_text(reference.title) != wanted
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

    async def lookup_by_title_and_author(self, user_input: str) -> BookMetadata | None:
        """Recherche par titre et indice d'auteur avec ranking intelligent."""
        title_cand, author_cand = parse_query_title_and_author(user_input)
        clean_title_q = clean_title_query(title_cand)

        # Requête BNF avec le titre
        cql = f'bib.title all "{clean_title_q}"'
        records = await self._query_bnf_sru(
            cql, author_hint=author_cand, title_hint=clean_title_q
        )

        if not records and author_cand:
            # Essai plus large si pas de résultat
            cql_broad = f'bib.anywhere all "{clean_title_q}"'
            records = await self._query_bnf_sru(
                cql_broad, author_hint=author_cand, title_hint=clean_title_q
            )

        if records:
            return self._attach_recommendations(records[0])

        fallback = await self._resolve_via_openlibrary(clean_title_q, author_cand)
        return fallback.best if fallback else None

    async def _query_bnf_sru(
        self,
        cql_query: str,
        author_hint: str | None = None,
        title_hint: str | None = None,
    ) -> list[BookMetadata]:
        """Interroge l'API SRU de la BNF. Absorbe les pannes : voir `_bnf_records`.

        Une liste vide y signifie « aucune notice », jamais « authorities muettes » :
        une panne ne doit pas ressembler à un refus. Seul `lookup_by_isbn_verdict`
        a besoin de savoir la différence.
        """
        records, _panne = await self._bnf_records(cql_query, author_hint, title_hint)
        return records

    async def _bnf_records(
        self,
        cql_query: str,
        author_hint: str | None = None,
        title_hint: str | None = None,
    ) -> tuple[list[BookMetadata], str | None]:
        """Notices SRU, et le motif de panne si l'autorité n'a pas répondu.

        Le second élément est ce qui manque à une liste vide pour être une réponse :
        sans lui, `[]` dit aussi bien « la BNF ignore cet ISBN » que « la requête a
        échoué », et l'appelant qui doit rendre compte ne peut pas trancher.
        """
        params = {
            "version": "1.2",
            "operation": "searchRetrieve",
            "query": cql_query,
            "recordSchema": "dublincore",
            "maximumRecords": "10",
        }
        url = f"{self.BNF_SRU_URL}?{urllib.parse.urlencode(params)}"

        results: list[tuple[float, BookMetadata]] = []
        try:
            client = self.http_client or get_shared_http_client()
            resp = await client.get(
                url,
                timeout=self.timeout_seconds,
                headers={"User-Agent": "BotRadar/1.0"},
            )
            if resp.status_code != 200:
                logger.warning("BNF SRU returned status %d", resp.status_code)
                return [], f"statut HTTP {resp.status_code}"

            root = ET.fromstring(resp.content)
            for record in root.findall(".//{http://www.loc.gov/zing/srw/}recordData"):
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
                formats = [f.text for f in record.findall(
                    ".//{http://purl.org/dc/elements/1.1/}format") if f.text]

                if not titles:
                    continue

                isbn13: str | None = None
                for ident in identifiers:
                    if "ISBN" in ident.upper():
                        c_isbn = re.sub(r"[^0-9X]", "", ident.upper())
                        if len(c_isbn) == 13:
                            isbn13 = c_isbn
                            break
                        elif len(c_isbn) == 10 and not isbn13:
                            converted = isbn10_to_isbn13(c_isbn)
                            isbn13 = converted or c_isbn

                if not isbn13:
                    continue

                main_title = clean_bnf_title(titles[0])
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
                if isbn13.startswith("9782") or isbn13.startswith("2"):
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
                )
                results.append((score, meta))

        except Exception as exc:
            logger.warning("Erreur lors de la requête BNF SRU (%s) : %s", cql_query, exc)
            return [], f"{type(exc).__name__}: {exc}"

        results.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in results], None

    async def _resolve_via_openlibrary(
        self, title: str, author_hint: str | None = None
    ) -> BookResolution | None:
        """Résolution de repli : OpenLibrary ne répond que si la BNF n'a rien donné.

        La confiance est moindre (`MEDIUM`) et la provenance est remontée jusqu'au
        récap Telegram : l'utilisateur doit savoir que l'ISBN vient d'une autre source.
        """
        params: dict[str, str] = {"title": title}
        if author_hint:
            params["author"] = author_hint

        works = await self._openlibrary_works(params)
        candidates = [
            work for work in works
            if title_similarity(work.meta.title, title) >= TITLE_SIMILARITY_MIN
        ]
        if not candidates:
            return None

        best = candidates[0].meta
        alternatives = [
            replace(best, isbn13=isbn)
            for isbn in candidates[0].edition_isbns
            if isbn != best.isbn13 and isbn.startswith(FRENCH_ISBN_PREFIXES)
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
        )

    async def _openlibrary_works(self, params: dict[str, str]) -> list[_OpenLibraryWork]:
        """Notices OpenLibrary correspondant à une recherche, triées par pertinence."""
        docs = await self._query_openlibrary(params)
        title_hint = params.get("title")
        author_hint = params.get("author")
        expected_isbn = normalize_isbn(params.get("isbn"))

        scored: list[tuple[float, _OpenLibraryWork]] = []
        for doc in docs:
            work = self._build_openlibrary_work(doc, expected_isbn)
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
        return [work for _score, work in scored]

    async def _query_openlibrary(self, params: dict[str, str]) -> list[dict[str, Any]]:
        """Interroge l'API Search d'OpenLibrary (aucune clé, aucune dépendance)."""
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
            if resp.status_code != 200:
                logger.warning("OpenLibrary a renvoyé le statut %d", resp.status_code)
                return []
            payload = resp.json()
        except Exception as exc:
            logger.warning("Erreur lors de la requête OpenLibrary : %s", exc)
            return []

        docs = payload.get("docs") if isinstance(payload, dict) else None
        if not isinstance(docs, list):
            return []
        return [doc for doc in docs if isinstance(doc, dict)]

    def _build_openlibrary_work(
        self, doc: dict[str, Any], expected_isbn: str | None
    ) -> _OpenLibraryWork | None:
        """Convertit un document OpenLibrary en notice avec l'ISBN pertinent retenu."""
        title = str(doc.get("title") or "").strip()
        if not title:
            return None

        edition_isbns = self._valid_openlibrary_isbns(doc.get("isbn"))
        isbn13 = self._select_openlibrary_isbn(edition_isbns, expected_isbn)
        if not isbn13:
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
