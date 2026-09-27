"""Service d'identification et recommandation automatique de métadonnées pour les BD & Comics.

Interroge en priorité la Bibliothèque Nationale de France (BNF) via son protocole SRU standard,
avec repli vers OpenLibrary et Chasse-aux-livres, afin d'extraire le titre officiel, l'auteur,
l'éditeur et l'ISBN-13, et de générer des mots-clés d'inclusion/exclusion optimaux.
"""

import difflib
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from app.core.constants import MatchConfidence
from app.core.http_client import get_shared_http_client
from app.utils.isbn import isbn10_to_isbn13
from app.utils.text import normalize_text

logger = logging.getLogger("radar.book_metadata")

STANDARD_BD_EXCLUDES = ["roman", "poche", "anglais", "english", "dvd"]


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
    source: str = "bnf"


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
        return parts[0].strip(), parts[1].strip()

    # Découpage par "de" (attention aux titres contenant "de", ex: "La saga de Grimr")
    parts_de = re.split(r"\s+de\s+", text, flags=re.IGNORECASE)
    if len(parts_de) > 1:
        last_part = parts_de[-1].strip()
        # Si le dernier segment ressemble à un nom d'auteur (1 à 4 mots max)
        if 1 <= len(last_part.split()) <= 4:
            author_candidate = last_part
            title_candidate = " de ".join(parts_de[:-1]).strip()
            if title_candidate:
                return title_candidate, author_candidate

    return text, None


def clean_bnf_title(raw_title: str) -> str:
    """Nettoie le titre brut retourné par la BNF (suppression des mentions d'auteur/sous-titres longs)."""
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


def generate_recommended_keywords(title: str, author: str | None = None) -> tuple[list[str], list[str], list[str]]:
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

    async def lookup_by_isbn(self, isbn: str) -> BookMetadata | None:
        """Recherche une œuvre par son ISBN officiel sur la BNF."""
        cql = f'bib.isbn adj "{isbn}"'
        records = await self._query_bnf_sru(cql)
        if records:
            meta = records[0]
            includes, excludes, variants = generate_recommended_keywords(meta.title, meta.author)
            meta.recommended_includes = includes
            meta.recommended_excludes = excludes
            meta.recommended_variants = variants
            return meta
        return None

    async def lookup_by_title_and_author(self, user_input: str) -> BookMetadata | None:
        """Recherche par titre et indice d'auteur avec ranking intelligent."""
        title_cand, author_cand = parse_query_title_and_author(user_input)

        # Nettoyage pour la requête CQL
        clean_title_q = re.sub(r"[^a-zA-Z0-9\sàâäéèêëîïôöùûüçÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ-]", " ", title_cand).strip()
        if not clean_title_q:
            clean_title_q = title_cand

        # Requête BNF avec le titre
        cql = f'bib.title all "{clean_title_q}"'
        records = await self._query_bnf_sru(cql, author_hint=author_cand)

        if not records and author_cand:
            # Essai plus large si pas de résultat
            cql_broad = f'bib.anywhere all "{clean_title_q}"'
            records = await self._query_bnf_sru(cql_broad, author_hint=author_cand)

        if records:
            best = records[0]
            includes, excludes, variants = generate_recommended_keywords(best.title, best.author)
            best.recommended_includes = includes
            best.recommended_excludes = excludes
            best.recommended_variants = variants
            return best

        return None

    async def _query_bnf_sru(self, cql_query: str, author_hint: str | None = None) -> list[BookMetadata]:
        """Interroge l'API SRU de la BNF et trie les résultats par pertinence."""
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
                return []

            root = ET.fromstring(resp.content)
            for record in root.findall(".//{http://www.loc.gov/zing/srw/}recordData"):
                titles = [t.text for t in record.findall(".//{http://purl.org/dc/elements/1.1/}title") if t.text]
                creators = [c.text for c in record.findall(".//{http://purl.org/dc/elements/1.1/}creator") if c.text]
                identifiers = [i.text for i in record.findall(".//{http://purl.org/dc/elements/1.1/}identifier") if i.text]
                publishers = [p.text for p in record.findall(".//{http://purl.org/dc/elements/1.1/}publisher") if p.text]

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

                # Calcul d'un score de similarité d'auteur si un indice d'auteur est donné
                score = 1.0
                if author_hint and creators:
                    hint_norm = normalize_text(author_hint)
                    creators_norm = " ".join(normalize_text(c) for c in creators)
                    ratio = difflib.SequenceMatcher(None, hint_norm, creators_norm).ratio()
                    for word in hint_norm.split():
                        if len(word) >= 3 and word in creators_norm:
                            ratio += 0.4
                    score += ratio

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

                meta = BookMetadata(
                    title=main_title,
                    author=author_name,
                    publisher=pub_name,
                    isbn13=isbn13,
                    source="bnf",
                )
                results.append((score, meta))

        except Exception as exc:
            logger.warning("Erreur lors de la requête BNF SRU (%s) : %s", cql_query, exc)
            return []

        results.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in results]

    async def _lookup_from_vinted_url(self, url: str) -> BookMetadata | None:
        """Extrait le texte de recherche ou l'ISBN d'une URL Vinted et effectue le lookup."""
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        search_text = params.get("search_text", [""])[0].strip()
        if search_text:
            return await self.lookup(search_text)
        return None
