"""Troisième source d'identification d'un ouvrage : Google Books.

La BNF reste la seule autorité ISBN du projet (ADR-014). Google Books n'est donc
interrogé **qu'après** la BNF et OpenLibrary, et sa notice ne renverse jamais une
notice BNF : elle comble un silence. Comme pour OpenLibrary, sa provenance remonte
jusqu'au récap (« Source Google Books (repli BNF) »), et sa confiance est `MEDIUM`.

Le module ne fait que la frontière HTTP et la lecture de la charge utile : le
classement des notices et la décision d'identifier l'ouvrage appartiennent à
`app/services/book_metadata_service.py`, qui sait ce qu'est une notice de référence.
Il n'importe donc rien du service, et n'invente pas une seconde idée de ce qu'est
un livre.

Google Books ne répond rien sans clé (`429` sur le quota anonyme) : sans
`GOOGLE_BOOKS_API_KEY`, la source n'existe pas — elle n'est ni interrogée, ni
annoncée comme consultée.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.http_client import get_shared_http_client
from app.utils.isbn import isbn_from_identifier

logger = logging.getLogger("radar.google_books")

GOOGLE_BOOKS_VOLUMES_URL = "https://www.googleapis.com/books/v1/volumes"

# Cinq volumes suffisent : la source ne se substitue à personne, elle rend ce que
# les deux autorités n'ont pas su dire. Au-delà, on paie une charge utile plus
# grosse pour des notices que le filtre de similarité écartera de toute façon.
MAX_RESULTS = 5


@dataclass(frozen=True)
class GoogleVolume:
    """Notice brute de Google Books, telle que l'API la rend.

    `isbns` porte les codes de la notice normalisés en ISBN-13 (une notice Google
    expose souvent l'ISBN-10 et l'ISBN-13 de la même édition) : c'est ce qui permet
    à l'appelant de vérifier qu'une notice atteste bien le code qu'il a demandé,
    au lieu de supposer que la réponse à une requête par ISBN en porte un.
    """

    title: str
    author: str | None = None
    publisher: str | None = None
    isbns: tuple[str, ...] = ()
    publication_year: int | None = None
    page_count: int | None = None

    @property
    def isbn13(self) -> str | None:
        """Code ISBN-13 de la notice, ou `None` si elle n'en porte aucun."""
        return next((code for code in self.isbns if len(code) == 13), None)


class GoogleBooksClient:
    """Client de l'API Volumes de Google Books.

    Une clé absente n'est pas une panne : `is_configured` vaut `False`, toutes les
    recherches rendent une liste vide sans appel réseau, et l'appelant peut dire
    honnêtement quelles autorités il a réellement interrogées.
    """

    def __init__(
        self,
        api_key: str | None = None,
        timeout_seconds: float = 8.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout_seconds = timeout_seconds
        self.http_client = http_client

    @property
    def is_configured(self) -> bool:
        """Une clé est-elle configurée ? Sans elle, la source n'interroge rien."""
        return bool(self.api_key)

    async def search_by_title(
        self, title: str, author_hint: str | None = None
    ) -> list[GoogleVolume]:
        """Volumes dont le titre est proche de celui demandé, les plus pertinents d'abord."""
        query = f'intitle:"{_clean_query_term(title)}"'
        if author_hint and author_hint.strip():
            query += f' inauthor:"{_clean_query_term(author_hint)}"'
        return await self._fetch(query)

    async def search_by_isbn(self, isbn: str) -> list[GoogleVolume]:
        """Volumes portant ce code, tels que Google Books les rend."""
        return await self._fetch(f"isbn:{isbn}")

    async def _fetch(self, query: str) -> list[GoogleVolume]:
        """Charge utile brute, absorbée : une panne rend une liste vide, jamais une exception."""
        if not self.is_configured:
            return []

        params = {
            "q": query,
            "maxResults": str(MAX_RESULTS),
            # La clé voyage en paramètre, jamais dans un message de log : le formatteur
            # JSON du projet masque déjà `key=`, et rien ici ne journalise l'URL.
            "key": self.api_key,
        }
        try:
            client = self.http_client or get_shared_http_client()
            resp = await client.get(
                GOOGLE_BOOKS_VOLUMES_URL,
                params=params,
                timeout=self.timeout_seconds,
                headers={"User-Agent": "BotRadar/1.0"},
            )
            if resp.status_code != 200:
                logger.warning("Google Books a renvoyé le statut %d", resp.status_code)
                return []
            payload = resp.json()
        except Exception as exc:
            logger.warning("Erreur lors de la requête Google Books : %s", exc)
            return []

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        volumes = (self._parse_item(item) for item in items if isinstance(item, dict))
        return [volume for volume in volumes if volume is not None]

    @staticmethod
    def _parse_item(item: dict[str, Any]) -> GoogleVolume | None:
        """Un volume de l'API en notice, ou `None` si elle ne nomme aucun ouvrage."""
        info = item.get("volumeInfo")
        if not isinstance(info, dict):
            return None
        title = str(info.get("title") or "").strip()
        if not title:
            return None

        authors = info.get("authors")
        author = (
            str(authors[0]).strip()
            if isinstance(authors, list) and authors and str(authors[0]).strip()
            else None
        )
        publisher = str(info.get("publisher") or "").strip() or None
        pages = info.get("pageCount")
        return GoogleVolume(
            title=title,
            author=author,
            publisher=publisher,
            isbns=_identifiers_to_isbns(info.get("industryIdentifiers")),
            publication_year=_publication_year(info.get("publishedDate")),
            page_count=pages if isinstance(pages, int) else None,
        )


def _clean_query_term(value: str) -> str:
    """Terme de requête sans guillemet ni retour ligne : la syntaxe de `q` est fragile.

    Un titre qui contient lui-même des guillemets (« Les "Enfants" perdus ») fermerait
    la valeur et ferait échouer la requête entière : les guillemets disparaissent, et
    les espaces qu'ils laissent sont réduits pour que le terme reste lisible.
    """
    without_quotes = re.sub(r'["\r\n]+', " ", value)
    return re.sub(r"\s+", " ", without_quotes).strip()


def _identifiers_to_isbns(raw: Any) -> tuple[str, ...]:
    """Codes ISBN-13 exploitables d'une notice, dédupliqués et dans l'ordre de la source."""
    if not isinstance(raw, list):
        return ()
    codes: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        code = isbn_from_identifier(str(entry.get("identifier") or ""))
        if code and code not in codes:
            codes.append(code)
    return tuple(codes)


def _publication_year(raw: Any) -> int | None:
    """Année d'une date Google Books (« 2026 », « 2026-09-23 », « 2026-09 »)."""
    match = re.search(r"(1[89]\d{2}|20\d{2})", str(raw or ""))
    return int(match.group(1)) if match else None
