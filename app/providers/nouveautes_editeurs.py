"""Source déclarée : les annonces de parution de « Nouveautés Éditeurs » (BnF).

La BNF reste la seule autorité d'identité (ADR-014, ADR-055). Ce site ne publie pas
le catalogue, mais les déclarations de parution adressées au dépôt légal : un livre
annoncé là peut être absent du catalogue. Le fournisseur est donc interrogé après la
BNF, OpenLibrary et Google Books, ne renverse jamais une notice BNF, et rend un
`ProviderQueryResult` typé — les règles d'admission vivent dans
`app/services/book_metadata_service.py`.

Conditions d'utilisation : la BnF réserve la réutilisation substantielle de cette
base à une autorisation préalable. Seules des consultations ponctuelles sont faites
ici, une par livre demandé, sans moissonnage, sans flux RSS, sans liste ni pagination.

Le site tient son état de recherche en session : sans amorçage, il rend sa liste par
défaut en enregistrant quand même le filtre (284 résultats au lieu de 1). Une
recherche ouvre une session neuve et émet au plus trois requêtes HTTP — amorçage,
recherche, fiche — sans reprise. Une réponse inattendue rend un échec nommé, jamais
une identité devinée.
"""

import html
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher

import httpx

from app.domain.enums import ProviderQueryStatus
from app.providers.google_books import ProviderQueryResult
from app.utils.isbn import (
    is_valid_isbn10,
    is_valid_isbn13,
    isbn10_to_isbn13,
    normalize_isbn,
)

logger = logging.getLogger("radar.nouveautes_editeurs")

BASE_URL = "https://nouveautes-editeurs.bnf.fr"
ACCUEIL_PATH = "/accueil"
USER_AGENT = "BotRadar/1.0"
DEFAULT_TIMEOUT_SECONDS = 4.0

# Une page de grille porte 20 déclarations : au-delà, la requête n'identifie plus
# un livre mais un rayon, et « aucune déclaration » ne serait plus une absence.
MAX_DECLARATIONS = 20

# Les deux états de dépôt légal affichés par le site. Toute autre valeur est
# conservée telle quelle : le rapport dit ce que le site dit, sans le traduire.
LEGAL_DEPOSIT_RECEIVED = "Reçu par la BnF"
LEGAL_DEPOSIT_ANNOUNCED = "Annoncé éditeur"

# Bornes de la lecture : deux appels logiques (recherche, fiche) et un amorçage
# de session, jamais davantage. Aucun de ces nombres n'est un réglage.
MAX_HTTP_CALLS = 3

_SEARCH_PARAMS = {
    "category": "FULL_TEXT",
    "action": "ADD_FILTER",
    "onRebondBySearch": "true",
    "modeAffichage": "gridMode",
    "page": "1",
    "minRange": "90",
    "maxRange": "90",
}

_RESULT_COUNT_RE = re.compile(r">\s*(\d+)\s+r[ée]sultats?")
_DECLARATION_ID_RE = re.compile(r"id_declaration=(\d+)")
_TITLE_SLUG_RE = re.compile(r"id_declaration=(\d+)(?:&amp;|&)titre_livre=([^\"&]*)")
_FILTER_MARKER = '"filtersList":[{"category":"FULL_TEXT"'
_DATE_RE = re.compile(r'id="dateId" name="dateId" value="(\d{2})/(\d{2})/(\d{4})"')
_DEPOSIT_RE = re.compile(r'class="depot divFlex".*?<span>([^<]+)</span>', re.S)
_TITLE_RE = re.compile(r"<h1>(.*?)</h1>", re.S)
_AUTHOR_RE = re.compile(r'name="unAuteur\d+" value="([^"]*)"')
_PUBLISHER_RE = re.compile(r"Éditeur\s*:(.{0,400}?)</a>", re.S)
_COLLECTION_RE = re.compile(r'name="collID" value="([^"]*)"')
_DOCUMENT_TYPE_RE = re.compile(r'class="type divFlex[^"]*"[^>]*>(.*?)</div>', re.S)
_ISBN_RE = re.compile(r"<strong>ISBN\s*</strong>\s*([0-9Xx\- ]+)\s*</p>", re.S)
_EAN_RE = re.compile(r"<strong>EAN\s*</strong>\s*(\d+)\s*</p>", re.S)
_PRICE_RE = re.compile(r"Prix de vente au public \(TTC\)\s*:\s*([0-9][0-9.,]*)")
_FORMAT_RE = re.compile(
    r'class="type divFlex[^"]*"[^>]*>.*?</div>\s*(?:<!--.*?-->\s*)*<p>(.*?)</p>', re.S
)
_PAGES_RE = re.compile(r"(\d+)\s*pages?\b")


@dataclass(frozen=True)
class BookDeclaration:
    """Une déclaration de parution, telle que sa fiche la donne.

    `legal_deposit_status` est le libellé du site, sans traduction : « Reçu par la
    BnF » n'est pas une preuve de catalogage, et « Annoncé éditeur » n'est pas un
    refus. `isbn13` n'est renseigné que si le code imprimé passe le contrôle de
    clé : un ISBN faux n'est pas un ISBN. Tout champ absent reste `None`, et rien
    n'est déduit d'un autre champ.
    """

    declaration_id: str
    url: str
    title: str
    publication_date: date
    authors: tuple[str, ...] = ()
    publisher: str | None = None
    collection: str | None = None
    isbn13: str | None = None
    ean13: str | None = None
    price_ttc_cents: int | None = None
    pages: int | None = None
    format_detail: str | None = None
    legal_deposit_status: str | None = None
    document_type: str | None = None

    @property
    def author(self) -> str | None:
        """Premier auteur déclaré : le site n'attribue aucun rôle aux suivants."""
        return self.authors[0] if self.authors else None

    @property
    def is_printed_book(self) -> bool:
        """Le site distingue les supports (« Livre imprimé », « Partitions »…)."""
        return _plain(self.document_type or "").casefold().startswith("livre")


@dataclass(frozen=True)
class _SearchPage:
    """Ce qu'une page de résultats dit de l'exécution de la recherche."""

    declaration_count: int
    declaration_ids: tuple[str, ...]
    filter_visible: bool
    raw: str


class _SourceFailureError(Exception):
    """Échec nommé d'une des requêtes de la recherche (absorbé par l'appelant)."""

    def __init__(self, result: ProviderQueryResult[BookDeclaration]) -> None:
        super().__init__(result.error_code or "source_failure")
        self.result = result


class NouveautesEditeursClient:
    """Client des annonces de parution, à session neuve par recherche.

    Aucune clé n'est nécessaire : `is_configured` est toujours vrai. Le client
    n'ouvre pas de session partagée, parce que le site garde l'état de recherche
    en session — une session réutilisée répondrait à la question précédente. Le
    `client_factory` existe pour les transports simulés des tests, pas pour un
    cache applicatif.
    """

    name = "nouveautes_editeurs"

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._client_factory = client_factory or self._default_client

    @property
    def is_configured(self) -> bool:
        """Sans clé ni jeton, la source existe quand même : elle répond au public."""
        return True

    async def search_by_isbn_result(
        self, isbn13: str
    ) -> ProviderQueryResult[BookDeclaration]:
        """Déclarations portant ce code (le code seul, jamais un titre approchant)."""
        return await self._search_result(isbn13)

    async def search_by_title_result(
        self, title: str
    ) -> ProviderQueryResult[BookDeclaration]:
        """Déclarations dont un mot du titre est celui demandé.

        L'auteur n'entre pas dans la requête : il est un critère de vérification,
        pas un terme de recherche. L'ajouter au texte libre ferait perdre les
        déclarations dont la graphie d'auteur diffère de celle de l'utilisateur.
        """
        return await self._search_result(title)

    def _default_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    async def _search_result(self, value: str) -> ProviderQueryResult[BookDeclaration]:
        """Recherche bornée : amorçage, recherche, fiche — puis plus rien."""
        clean_value = " ".join(value.split())
        if not clean_value:
            logger.warning("Nouveautés Éditeurs : recherche sans terme")
            return ProviderQueryResult(
                status=ProviderQueryStatus.INVALID_RESPONSE, error_code="empty_query"
            )

        client = self._client_factory()
        calls = 0
        try:
            await self._get(client, BASE_URL + "/")
            calls += 1
            page = await self._post(client, clean_value)
            calls += 1
            search = _read_search(page)
            failure = _search_failure(search)
            if failure is not None:
                return failure
            if not search.declaration_ids:
                # Aucun identifiant, compteur à zéro et filtre visible : absence
                # réelle de déclaration pour ce terme, et non recherche perdue.
                return ProviderQueryResult(status=ProviderQueryStatus.SUCCESS)

            declaration_id = _pick_declaration(search, clean_value)
            fiche = await self._get(
                client, f"{BASE_URL}{ACCUEIL_PATH}?id_declaration={declaration_id}"
            )
            calls += 1
            declaration = parse_declaration(fiche, declaration_id)
            if declaration is None:
                logger.warning("Nouveautés Éditeurs : fiche de déclaration illisible")
                return ProviderQueryResult(
                    status=ProviderQueryStatus.INVALID_RESPONSE,
                    error_code="unexpected_declaration",
                    has_results=True,
                )
            return ProviderQueryResult(
                status=ProviderQueryStatus.SUCCESS,
                items=(declaration,),
                has_results=True,
            )
        except _SourceFailureError as failure:
            return failure.result
        except (httpx.TimeoutException, TimeoutError):
            logger.warning("Nouveautés Éditeurs : délai dépassé")
            return ProviderQueryResult(
                status=ProviderQueryStatus.TIMEOUT, error_code="timeout"
            )
        except Exception as exc:
            logger.warning(
                "Erreur lors de la requête Nouveautés Éditeurs : %s", type(exc).__name__
            )
            return ProviderQueryResult(
                status=ProviderQueryStatus.UNAVAILABLE, error_code=type(exc).__name__
            )
        finally:
            if calls > MAX_HTTP_CALLS:  # pragma: no cover - garde-fou, jamais atteint
                logger.error(
                    "Nouveautés Éditeurs : %d requêtes pour une recherche", calls
                )
            await client.aclose()

    async def _get(self, client: httpx.AsyncClient, url: str) -> str:
        return await self._request(client, "GET", url)

    async def _post(self, client: httpx.AsyncClient, value: str) -> str:
        """POST formulaire — corps urlencodé, jamais un paramètre d'URL."""
        return await self._request(
            client, "POST", BASE_URL + ACCUEIL_PATH, {"refineValue": value, **_SEARCH_PARAMS}
        )

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        data: dict[str, str] | None = None,
    ) -> str:
        """Le corps de la réponse, ou un échec nommé — jamais une exception qui fuit.

        Rien n'est journalisé de l'URL, des paramètres ou de la charge utile : les
        messages ne portent que des noms de classe et des codes contrôlés.
        """
        try:
            response = await client.request(
                method, url, data=data, timeout=self.timeout_seconds
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise _SourceFailureError(
                ProviderQueryResult(
                    status=ProviderQueryStatus.TIMEOUT, error_code="timeout"
                )
            ) from exc
        except Exception as exc:
            raise _SourceFailureError(
                ProviderQueryResult(
                    status=ProviderQueryStatus.UNAVAILABLE,
                    error_code=type(exc).__name__,
                )
            ) from exc

        status_code = response.status_code
        if status_code in (401, 403):
            raise _SourceFailureError(
                ProviderQueryResult(
                    status=ProviderQueryStatus.UNAUTHORIZED,
                    error_code=f"http_{status_code}",
                )
            )
        if status_code == 429:
            raise _SourceFailureError(
                ProviderQueryResult(
                    status=ProviderQueryStatus.RATE_LIMITED, error_code="http_429"
                )
            )
        if status_code != 200:
            raise _SourceFailureError(
                ProviderQueryResult(
                    status=ProviderQueryStatus.UNAVAILABLE,
                    error_code=f"http_{status_code}",
                )
            )
        try:
            return response.text
        except Exception as exc:  # pragma: no cover - httpx ne lève pas ici
            raise _SourceFailureError(
                ProviderQueryResult(
                    status=ProviderQueryStatus.INVALID_RESPONSE, error_code="unreadable"
                )
            ) from exc


def _search_failure(search: _SearchPage) -> ProviderQueryResult[BookDeclaration] | None:
    """La recherche a-t-elle réellement été appliquée ?

    Sans amorçage de session, le site répond sa liste par défaut (constaté :
    « 284 résultats » et 20 identifiants) en enregistrant pourtant le filtre. Le
    compteur affiché est donc la seule preuve que la recherche a porté : trop de
    déclarations pour le terme demandé, ou zéro déclaration sans filtre visible,
    sont des défauts de protocole, jamais des absences.
    """
    if search.declaration_count > MAX_DECLARATIONS:
        logger.warning(
            "Nouveautés Éditeurs : %d déclarations pour une recherche ciblée",
            search.declaration_count,
        )
        return ProviderQueryResult(
            status=ProviderQueryStatus.INVALID_RESPONSE,
            error_code="unexpected_results",
            has_results=True,
        )
    if search.declaration_count and not search.declaration_ids:
        logger.warning("Nouveautés Éditeurs : compteur de résultats sans identifiant")
        return ProviderQueryResult(
            status=ProviderQueryStatus.INVALID_RESPONSE,
            error_code="unexpected_structure",
            has_results=True,
        )
    if not search.declaration_count and not search.filter_visible:
        logger.warning("Nouveautés Éditeurs : recherche non appliquée")
        return ProviderQueryResult(
            status=ProviderQueryStatus.INVALID_RESPONSE,
            error_code="search_not_applied",
        )
    if not search.declaration_count and search.declaration_ids:
        logger.warning("Nouveautés Éditeurs : identifiants sans compteur")
        return ProviderQueryResult(
            status=ProviderQueryStatus.INVALID_RESPONSE,
            error_code="unexpected_structure",
            has_results=True,
        )
    return None


def _read_search(page: str) -> _SearchPage:
    """Compteur affiché, identifiants de la page et état du filtre."""
    count = _RESULT_COUNT_RE.search(page)
    ids = tuple(dict.fromkeys(_DECLARATION_ID_RE.findall(page)))
    return _SearchPage(
        declaration_count=int(count.group(1)) if count else 0,
        declaration_ids=ids,
        filter_visible=_FILTER_MARKER in page,
        raw=page,
    )


def _pick_declaration(search: _SearchPage, value: str) -> str:
    """La fiche à lire quand plusieurs déclarations répondent.

    Une seule fiche est lue : le fournisseur ne classe pas les œuvres, mais il doit
    choisir laquelle ouvrir. Le titre porté par le lien de la liste départage les
    identifiants, et l'ordre du site tranche à égalité. L'appelant reste seul juge
    de l'identité, avec le code et le titre lus sur la fiche.
    """
    if len(search.declaration_ids) == 1:
        return search.declaration_ids[0]
    if normalize_isbn(value):
        return search.declaration_ids[0]
    slugs = {
        declaration_id: slug.replace("_", " ")
        for declaration_id, slug in _TITLE_SLUG_RE.findall(search.raw)
    }
    best_id = search.declaration_ids[0]
    best_score = _similarity(slugs.get(best_id, ""), value)
    for declaration_id in search.declaration_ids[1:]:
        score = _similarity(slugs.get(declaration_id, ""), value)
        if score > best_score:
            best_id, best_score = declaration_id, score
    return best_id


def parse_declaration(page: str, declaration_id: str) -> BookDeclaration | None:
    """La déclaration décrite par une fiche, ou `None` si elle n'est pas lisible.

    Deux éléments sont exigés — un titre et une date de parution — parce que sans
    eux il n'y a pas de déclaration de livre. Tous les autres champs sont
    facultatifs et restent `None` quand la fiche les tait : un prix absent n'est
    pas un prix nul, et une collection absente ne se déduit pas de l'éditeur.
    """
    date_match = _DATE_RE.search(page)
    title_match = _TITLE_RE.search(page)
    if date_match is None or title_match is None:
        return None
    title = _plain(title_match.group(1))
    if not title:
        return None
    try:
        publication_date = date(
            int(date_match.group(3)), int(date_match.group(2)), int(date_match.group(1))
        )
    except ValueError:
        return None

    ean_raw = _normalize_code(_EAN_RE.search(page))
    isbn13 = _isbn13_of(_ISBN_RE.search(page)) or _isbn13_of(_EAN_RE.search(page))

    deposit = _DEPOSIT_RE.search(page)
    document_type = _DOCUMENT_TYPE_RE.search(page)
    format_line = _FORMAT_RE.search(page)
    pages = _PAGES_RE.search(_plain(format_line.group(1))) if format_line else None
    return BookDeclaration(
        declaration_id=declaration_id,
        url=f"{BASE_URL}{ACCUEIL_PATH}?id_declaration={declaration_id}",
        title=title,
        publication_date=publication_date,
        authors=tuple(
            author for author in (_plain(raw) for raw in _AUTHOR_RE.findall(page)) if author
        ),
        publisher=_publisher(page),
        collection=_attribute(page, _COLLECTION_RE),
        isbn13=isbn13,
        ean13=ean_raw if ean_raw and len(ean_raw) == 13 else None,
        price_ttc_cents=_price_cents(page),
        pages=int(pages.group(1)) if pages else None,
        format_detail=_plain(format_line.group(1)) if format_line else None,
        legal_deposit_status=_plain(deposit.group(1)) if deposit else None,
        document_type=_plain(document_type.group(1)) if document_type else None,
    )


def _attribute(page: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(page)
    return _plain(match.group(1)) or None if match else None


def _publisher(page: str) -> str | None:
    """L'éditeur affiché après « Éditeur : », sans son lien ni son identifiant."""
    match = _PUBLISHER_RE.search(page)
    if match is None:
        return None
    return _plain(re.sub(r"<[^>]+>", " ", match.group(1))) or None


def _normalize_code(match: re.Match[str] | None) -> str | None:
    if match is None:
        return None
    return normalize_isbn(match.group(1)) or None


def _isbn13_of(match: re.Match[str] | None) -> str | None:
    """ISBN-13 du code affiché, clé de contrôle vérifiée — sinon rien."""
    code = _normalize_code(match)
    if code is None:
        return None
    if len(code) == 10:
        return isbn10_to_isbn13(code) if is_valid_isbn10(code) else None
    return code if is_valid_isbn13(code) else None


def _price_cents(page: str) -> int | None:
    match = _PRICE_RE.search(page)
    if match is None:
        return None
    return _cents(match.group(1))


def _cents(raw: str) -> int | None:
    """« 35.00 » ou « 35,00 » en centimes, sans arrondi inventé."""
    whole, _, fraction = raw.replace(",", ".").partition(".")
    if not whole.isdigit():
        return None
    fraction = (fraction + "00")[:2]
    if not fraction.isdigit():
        return None
    return int(whole) * 100 + int(fraction)


def _plain(fragment: str) -> str:
    """Texte d'un fragment HTML : entités résolues, balises retirées, espaces unis."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _similarity(first: str, second: str) -> float:
    return SequenceMatcher(None, first.casefold(), second.casefold()).ratio()
