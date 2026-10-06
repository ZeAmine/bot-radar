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
annoncée comme consultée (`ProviderQueryStatus.DISABLED`).

Lot 1 : chaque interrogation rend un `ProviderQueryResult` typé au lieu d'une
liste nue. Une liste vide ne dit plus rien seule — `success` + `items=()` est
une absence de résultat, tout autre statut une panne à ne pas confondre avec
un vide. Les méthodes historiques (`search_by_title`, `search_by_isbn`)
délèguent au résultat typé et ne rendent que les volumes, comportement
inchangé pour leurs appelants.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.http_client import get_shared_http_client
from app.domain.enums import ProviderQueryStatus
from app.utils.isbn import isbn_from_identifier

logger = logging.getLogger("radar.google_books")

GOOGLE_BOOKS_VOLUMES_URL = "https://www.googleapis.com/books/v1/volumes"

# Cinq volumes suffisent : la source ne se substitue à personne, elle rend ce que
# les deux autorités n'ont pas su dire. Au-delà, on paie une charge utile plus
# grosse pour des notices que le filtre de similarité écartera de toute façon.
MAX_RESULTS = 5


@dataclass(frozen=True)
class ProviderQueryResult[T]:
    """Issue typée d'une interrogation de fournisseur.

    `retry_after_seconds` ne reprend que le `Retry-After` réellement renvoyé
    (aucun délai inventé) ; `error_code` nomme la panne sans jamais porter la
    clé API, l'URL complète ni de charge utile brute.
    """

    items: tuple[T, ...] = ()
    status: ProviderQueryStatus = ProviderQueryStatus.SUCCESS
    retry_after_seconds: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class GoogleVolume:
    """Notice brute de Google Books, telle que l'API la rend.

    `isbns` porte les codes de la notice normalisés en ISBN-13 (une notice Google
    expose souvent l'ISBN-10 et l'ISBN-13 de la même édition) : c'est ce qui permet
    à l'appelant de vérifier qu'une notice atteste bien le code qu'il a demandé,
    au lieu de supposer que la réponse à une requête par ISBN en porte un.

    `authors` conserve tous les noms rendus, sans aucun rôle : attribuer
    « scénariste », « dessinateur » ou « traducteur » depuis cette liste serait
    inventer une information que Google ne donne pas. `author` reste le premier
    nom pour les appelants historiques. `print_type` reprend `printType` tel
    quel : `BOOK` ne prouve pas une édition papier. `has_ebook` ne dit que la
    disponibilité ebook indiquée par Google (`saleInfo.isEbook`, sinon
    `accessInfo`), jamais la nature matérielle de l'œuvre. Tout absent reste
    `None` (ou tuple vide) : le volume ne devine rien.
    """

    title: str
    author: str | None = None
    publisher: str | None = None
    isbns: tuple[str, ...] = ()
    publication_year: int | None = None
    page_count: int | None = None
    authors: tuple[str, ...] = ()
    language: str | None = None
    description: str | None = None
    categories: tuple[str, ...] = ()
    print_type: str | None = None
    has_ebook: bool | None = None
    cover_url: str | None = None

    @property
    def isbn13(self) -> str | None:
        """Code ISBN-13 de la notice, ou `None` si elle n'en porte aucun."""
        return next((code for code in self.isbns if len(code) == 13), None)


class GoogleBooksClient:
    """Client de l'API Volumes de Google Books.

    Une clé absente n'est pas une panne : `is_configured` vaut `False`, toutes les
    recherches rendent un résultat `disabled` sans appel réseau, et l'appelant peut
    dire honnêtement quelles autorités il a réellement interrogées.
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

    async def search_by_title_result(
        self, title: str, author_hint: str | None = None
    ) -> ProviderQueryResult[GoogleVolume]:
        """Recherche ciblée, typée : `intitle:` + `inauthor:` éventuel."""
        query = f'intitle:"{_clean_query_term(title)}"'
        if author_hint and author_hint.strip():
            query += f' inauthor:"{_clean_query_term(author_hint)}"'
        return await self._fetch_result(query)

    async def search_by_isbn_result(self, isbn: str) -> ProviderQueryResult[GoogleVolume]:
        """Recherche par code, typée : aucun repli libre sur un ISBN seul."""
        return await self._fetch_result(f"isbn:{isbn}")

    async def search_free_result(
        self, title: str, author_hint: str | None = None
    ) -> ProviderQueryResult[GoogleVolume]:
        """Recherche libre, typée : les mots du titre et de l'auteur, sans opérateur.

        Repli après une recherche ciblée vide uniquement : `blacksad juan diaz
        canales`, jamais une confirmation automatique — le filtrage métier du
        service reste seul juge des candidats rendus ici.
        """
        terms = _clean_query_term(title)
        if author_hint and author_hint.strip():
            terms += f" {_clean_query_term(author_hint)}"
        return await self._fetch_result(terms)

    async def search_by_title(
        self, title: str, author_hint: str | None = None
    ) -> list[GoogleVolume]:
        """Volumes dont le titre est proche de celui demandé, les plus pertinents d'abord."""
        return list((await self.search_by_title_result(title, author_hint)).items)

    async def search_by_isbn(self, isbn: str) -> list[GoogleVolume]:
        """Volumes portant ce code, tels que Google Books les rend."""
        return list((await self.search_by_isbn_result(isbn)).items)

    async def _fetch(self, query: str) -> list[GoogleVolume]:
        """Charge utile brute, absorbée : une panne rend une liste vide, jamais une exception."""
        return list((await self._fetch_result(query)).items)

    async def _fetch_result(self, query: str) -> ProviderQueryResult[GoogleVolume]:
        """Charge utile typée : le statut distingue le vide d'une panne.

        La clé voyage en paramètre, jamais dans un message de log : le formatteur
        JSON du projet masque déjà `key=`, et rien ici ne journalise l'URL, les
        paramètres ou la charge utile. Les exceptions journalisées ne portent que
        leur nom de classe, pas leur message (qui peut citer l'URL appelée).
        """
        if not self.is_configured:
            return ProviderQueryResult(status=ProviderQueryStatus.DISABLED)

        params = {
            "q": query,
            "maxResults": str(MAX_RESULTS),
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
        except (httpx.TimeoutException, TimeoutError):
            logger.warning("Google Books : délai dépassé")
            return ProviderQueryResult(
                status=ProviderQueryStatus.TIMEOUT, error_code="timeout"
            )
        except Exception as exc:
            logger.warning("Erreur lors de la requête Google Books : %s", type(exc).__name__)
            return ProviderQueryResult(
                status=ProviderQueryStatus.UNAVAILABLE,
                error_code=type(exc).__name__,
            )

        if resp.status_code in (401, 403):
            logger.warning("Google Books a refusé l'authentification : %d", resp.status_code)
            return ProviderQueryResult(
                status=ProviderQueryStatus.UNAUTHORIZED,
                error_code=f"http_{resp.status_code}",
            )
        if resp.status_code == 429:
            logger.warning("Google Books : quota dépassé (429)")
            return ProviderQueryResult(
                status=ProviderQueryStatus.RATE_LIMITED,
                retry_after_seconds=_retry_after_seconds(resp),
                error_code="http_429",
            )
        if resp.status_code != 200:
            logger.warning("Google Books a renvoyé le statut %d", resp.status_code)
            return ProviderQueryResult(
                status=ProviderQueryStatus.UNAVAILABLE,
                error_code=f"http_{resp.status_code}",
            )
        try:
            payload = resp.json()
        except Exception:
            logger.warning("Google Books : charge utile JSON illisible")
            return ProviderQueryResult(
                status=ProviderQueryStatus.INVALID_RESPONSE, error_code="invalid_json"
            )

        items = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, list):
            volumes = (
                self._parse_item(item) for item in items if isinstance(item, dict)
            )
            return ProviderQueryResult(
                status=ProviderQueryStatus.SUCCESS,
                items=tuple(volume for volume in volumes if volume is not None),
            )
        if (
            isinstance(payload, dict)
            and items is None
            and payload.get("totalItems", 0) == 0
        ):
            # `{"totalItems": 0}` sans `items` est un succès sans résultat, pas
            # une structure inattendue : Google omet la clé quand il n'a rien.
            return ProviderQueryResult(status=ProviderQueryStatus.SUCCESS)
        logger.warning("Google Books : structure de réponse inattendue")
        return ProviderQueryResult(
            status=ProviderQueryStatus.INVALID_RESPONSE,
            error_code="unexpected_structure",
        )

    @staticmethod
    def _parse_item(item: dict[str, Any]) -> GoogleVolume | None:
        """Un volume de l'API en notice, ou `None` si elle ne nomme aucun ouvrage."""
        info = item.get("volumeInfo")
        if not isinstance(info, dict):
            return None
        title = str(info.get("title") or "").strip()
        if not title:
            return None

        pages = info.get("pageCount")
        authors = _clean_names(info.get("authors"))
        return GoogleVolume(
            title=title,
            author=authors[0] if authors else None,
            publisher=_clean_text(info.get("publisher")),
            isbns=_identifiers_to_isbns(info.get("industryIdentifiers")),
            publication_year=_publication_year(info.get("publishedDate")),
            page_count=pages if isinstance(pages, int) else None,
            authors=authors,
            language=_clean_text(info.get("language")),
            description=_clean_text(info.get("description")),
            categories=_clean_names(info.get("categories")),
            print_type=_clean_text(info.get("printType")),
            has_ebook=_ebook_available(info.get("accessInfo"), item.get("saleInfo")),
            cover_url=_cover_url(info.get("imageLinks")),
        )


def _clean_query_term(value: str) -> str:
    """Terme de requête sans guillemet ni retour ligne : la syntaxe de `q` est fragile.

    Un titre qui contient lui-même des guillemets (« Les "Enfants" perdus ») fermerait
    la valeur et ferait échouer la requête entière : les guillemets disparaissent, et
    les espaces qu'ils laissent sont réduits pour que le terme reste lisible.
    """
    without_quotes = re.sub(r'["\r\n]+', " ", value)
    return re.sub(r"\s+", " ", without_quotes).strip()


def _clean_text(raw: Any) -> str | None:
    """Chaîne non vide telle que l'API la rend, ou `None` si absente."""
    text = str(raw).strip() if raw is not None else ""
    return text or None


def _clean_names(raw: Any) -> tuple[str, ...]:
    """Noms d'une liste de l'API, nettoyés et dédupliqués, sans rôle inventé."""
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for entry in raw:
        name = str(entry).strip() if entry is not None else ""
        if name and name not in names:
            names.append(name)
    return tuple(names)


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


def _ebook_available(access_info: Any, sale_info: Any) -> bool | None:
    """Disponibilité ebook indiquée par Google, rien de plus.

    `saleInfo.isEbook` quand c'est un booléen, sinon le OU des disponibilités
    `accessInfo.epub` / `accessInfo.pdf`. Sans aucune de ces informations,
    `None` : l'absence de l'indicateur n'est pas une absence d'ebook.
    """
    if isinstance(sale_info, dict) and isinstance(sale_info.get("isEbook"), bool):
        return bool(sale_info["isEbook"])
    if isinstance(access_info, dict):
        entries = [access_info.get("epub"), access_info.get("pdf")]
        flags = [
            entry.get("isAvailable") for entry in entries if isinstance(entry, dict)
        ]
        stated = [flag for flag in flags if isinstance(flag, bool)]
        if stated:
            return any(stated)
    return None


def _cover_url(image_links: Any) -> str | None:
    """Couverture : `thumbnail` d'abord, `smallThumbnail` en repli, sinon rien."""
    if not isinstance(image_links, dict):
        return None
    return _clean_text(image_links.get("thumbnail")) or _clean_text(
        image_links.get("smallThumbnail")
    )


def _retry_after_seconds(resp: Any) -> int | None:
    """Le `Retry-After` réellement renvoyé, ou `None` s'il manque ou illisible."""
    headers: Any = getattr(resp, "headers", None)
    get: Any = getattr(headers, "get", None)
    raw: Any = get("retry-after") if callable(get) else None
    if raw is None:
        items: Any = getattr(headers, "items", None)
        if callable(items):
            try:
                pairs: Any = items()
            except Exception:
                pairs = []
            raw = next(
                (value for key, value in pairs if str(key).lower() == "retry-after"),
                None,
            )
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    return seconds if seconds >= 0 else None
