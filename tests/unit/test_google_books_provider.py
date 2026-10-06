"""Tests du troisième repli d'identification : la frontière HTTP de Google Books.

Le module ne décide de rien — il lit une charge utile et la rend — donc ce qu'il faut
verrouiller ici, c'est ce qu'il lit : un code reconnu, une année, une pagination, et
surtout **l'absence** de clé, qui doit valoir « pas de source » et non « pas de
réponse ».

Lot 1 : chaque interrogation rend un `ProviderQueryResult` typé. Une liste vide ne
dit plus rien seule — les tests ci-dessous verrouillent chaque statut, les
métadonnées étendues (auteurs sans rôles, langue, ebook, `printType` repris tel
quel) et la compatibilité des méthodes historiques.
"""

import logging

import httpx
import pytest

from app.domain.enums import ProviderQueryStatus
from app.providers.google_books import GoogleBooksClient


class _FakeResponse:
    def __init__(
        self,
        payload: dict[str, object] | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {}
        self._json_error = json_error

    def json(self) -> dict[str, object]:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _FakeHttp:
    """Client HTTP muet : rend la charge utile demandée et retient les requêtes."""

    def __init__(
        self,
        payload: dict[str, object] | None = None,
        status_code: int = 200,
        error: Exception | None = None,
        headers: dict[str, str] | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.error = error
        self.headers = headers
        self.json_error = json_error
        self.requests: list[dict[str, object]] = []

    async def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.requests.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return _FakeResponse(
            self.payload, self.status_code, self.headers, self.json_error
        )


def _item(**info: object) -> dict[str, object]:
    return {"volumeInfo": info}


@pytest.mark.asyncio
async def test_a_missing_key_means_no_source_rather_than_no_answer():
    """Sans clé, la source n'existe pas : aucun appel, donc aucune panne inventée."""
    http = _FakeHttp(payload={"items": [_item(title="Dailycieux")]})
    client = GoogleBooksClient(api_key="", http_client=http)  # type: ignore[arg-type]

    assert client.is_configured is False
    assert await client.search_by_title("Dailycieux") == []
    assert await client.search_by_isbn("9782812322495") == []
    assert http.requests == []


@pytest.mark.asyncio
async def test_a_volume_is_read_with_its_identifiers_normalised():
    """Une notice rend son titre, son auteur, son éditeur, son code et son format."""
    http = _FakeHttp(
        payload={
            "totalItems": 1,
            "items": [
                _item(
                    title="Dailycieux",
                    authors=["Whoogy's"],
                    publisher="Éditions du Chêne",
                    publishedDate="2026-09-23",
                    pageCount=240,
                    industryIdentifiers=[
                        {"type": "ISBN_10", "identifier": "0306406152"},
                        {"type": "ISBN_13", "identifier": "9780306406157"},
                    ],
                )
            ],
        }
    )
    client = GoogleBooksClient(api_key="cle-de-test", http_client=http)  # type: ignore[arg-type]

    volumes = await client.search_by_title("Dailycieux")

    assert len(volumes) == 1
    volume = volumes[0]
    assert volume.title == "Dailycieux"
    assert volume.author == "Whoogy's"
    assert volume.publisher == "Éditions du Chêne"
    # L'ISBN-10 est converti, l'ISBN-13 dédupliqué : un seul code, le bon.
    assert volume.isbns == ("9780306406157",)
    assert volume.isbn13 == "9780306406157"
    assert volume.publication_year == 2026
    assert volume.page_count == 240


@pytest.mark.asyncio
async def test_an_item_that_names_nothing_is_not_a_notice():
    """Un volume sans titre ou sans `volumeInfo` n'est pas une notice, et ne lève pas."""
    http = _FakeHttp(
        payload={
            "items": [
                {"id": "sans-volumeinfo"},
                _item(title="   "),
                "pas-un-objet",
                _item(title="Blacksad", authors=[]),
            ]
        }
    )
    client = GoogleBooksClient(api_key="cle-de-test", http_client=http)  # type: ignore[arg-type]

    volumes = await client.search_by_title("Blacksad")

    assert [volume.title for volume in volumes] == ["Blacksad"]
    assert volumes[0].author is None
    assert volumes[0].isbns == ()


@pytest.mark.asyncio
async def test_a_failed_request_is_absorbed_into_an_empty_answer():
    """Une panne ou un statut non 200 ne remontent jamais : la source se tait."""
    refus = GoogleBooksClient(
        api_key="cle-de-test",
        http_client=_FakeHttp(status_code=429),  # type: ignore[arg-type]
    )
    panne = GoogleBooksClient(
        api_key="cle-de-test",
        http_client=_FakeHttp(error=TimeoutError("délai dépassé")),  # type: ignore[arg-type]
    )

    assert await refus.search_by_title("Dailycieux") == []
    assert await panne.search_by_title("Dailycieux") == []


@pytest.mark.asyncio
async def test_the_author_hint_narrows_the_query_and_quotes_are_cleaned():
    """L'indice d'auteur restreint la requête, et un guillemet ne casse pas la syntaxe."""
    http = _FakeHttp(payload={"totalItems": 0})
    client = GoogleBooksClient(api_key="cle-de-test", http_client=http)  # type: ignore[arg-type]

    await client.search_by_title('Les "Enfants" perdus', "Whoogy's")

    query = str(http.requests[0]["params"]["q"])  # type: ignore[index]
    assert query == 'intitle:"Les Enfants perdus" inauthor:"Whoogy\'s"'

    await client.search_by_isbn("9782812322495")
    assert http.requests[1]["params"]["q"] == "isbn:9782812322495"  # type: ignore[index]


@pytest.mark.asyncio
async def test_the_key_never_leaves_the_request():
    """La clé part en paramètre de requête, jamais dans un message journalisé."""
    http = _FakeHttp(payload={"totalItems": 0})
    client = GoogleBooksClient(api_key="cle-secrete", http_client=http)  # type: ignore[arg-type]

    await client.search_by_title("Dailycieux")

    assert http.requests[0]["params"]["key"] == "cle-secrete"  # type: ignore[index]


@pytest.mark.asyncio
async def test_the_shared_project_client_is_used_when_none_is_injected(monkeypatch):
    """La source passe par le client HTTP partagé, jamais par un `AsyncClient` neuf."""
    partage = _FakeHttp(payload={"totalItems": 0})
    monkeypatch.setattr(
        "app.providers.google_books.get_shared_http_client", lambda: partage
    )
    client = GoogleBooksClient(api_key="cle-de-test")

    assert await client.search_by_title("Dailycieux") == []
    assert partage.requests, "le client partagé a porté la requête"


def _client(resultat: _FakeHttp) -> GoogleBooksClient:
    return GoogleBooksClient(api_key="cle-de-test", http_client=resultat)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_disabled_means_no_request_and_no_source():
    """Clé absente : `disabled`, zéro requête, zéro volume."""
    http = _FakeHttp(payload={"items": [_item(title="Dailycieux")]})
    client = GoogleBooksClient(api_key="  ", http_client=http)  # type: ignore[arg-type]

    titre = await client.search_by_title_result("Dailycieux")
    isbn = await client.search_by_isbn_result("9782812322495")
    libre = await client.search_free_result("Dailycieux")

    assert titre.status == ProviderQueryStatus.DISABLED
    assert isbn.status == ProviderQueryStatus.DISABLED
    assert libre.status == ProviderQueryStatus.DISABLED
    assert titre.items == isbn.items == libre.items == ()
    assert http.requests == []


@pytest.mark.asyncio
async def test_success_without_results_is_not_a_failure():
    """`{"totalItems": 0}` sans `items` : succès vide, pas une panne."""
    http = _FakeHttp(payload={"totalItems": 0})

    resultat = await _client(http).search_by_title_result("Ouvrage Introuvable")

    assert resultat.status == ProviderQueryStatus.SUCCESS
    assert resultat.items == ()
    assert resultat.retry_after_seconds is None
    assert resultat.error_code is None


@pytest.mark.asyncio
async def test_extended_metadata_keeps_everything_without_inventing_roles():
    """Tous les auteurs, la langue, l'ebook, le type et la couverture sont lus."""
    http = _FakeHttp(
        payload={
            "totalItems": 1,
            "items": [
                _item(
                    title="Blacksad",
                    authors=["Juan Díaz Canales", "Juanjo Guarnido"],
                    publisher="Dargaud",
                    publishedDate="2000-11",
                    pageCount=56,
                    language="fr",
                    description="Un chat détective.",
                    categories=["Comics & Graphic Novels"],
                    printType="BOOK",
                    industryIdentifiers=[
                        {"type": "ISBN_10", "identifier": "0306406152"},
                        {"type": "ISBN_13", "identifier": "9780306406157"},
                    ],
                    imageLinks={
                        "smallThumbnail": "http://exemple/petit.jpg",
                        "thumbnail": "http://exemple/grand.jpg",
                    },
                )
            ],
        }
    )

    resultat = await _client(http).search_by_title_result("Blacksad")

    assert resultat.status == ProviderQueryStatus.SUCCESS
    (volume,) = resultat.items
    assert volume.authors == ("Juan Díaz Canales", "Juanjo Guarnido")
    assert volume.author == "Juan Díaz Canales"
    assert volume.language == "fr"
    assert volume.description == "Un chat détective."
    assert volume.categories == ("Comics & Graphic Novels",)
    # `BOOK` est repris tel quel : il ne prouve pas une édition papier.
    assert volume.print_type == "BOOK"
    assert volume.cover_url == "http://exemple/grand.jpg"
    assert volume.isbns == ("9780306406157",)
    assert volume.publication_year == 2000


@pytest.mark.asyncio
async def test_ebook_availability_comes_from_sale_or_access_info():
    """`has_ebook` ne dit que la disponibilité indiquée, jamais la matière."""
    vente = _FakeHttp(
        payload={
            "items": [
                {"volumeInfo": {"title": "T1"}, "saleInfo": {"isEbook": True}},
            ]
        }
    )
    acces = _FakeHttp(
        payload={
            "items": [
                _item(
                    title="T2",
                    accessInfo={"epub": {"isAvailable": False}, "pdf": {"isAvailable": True}},
                )
            ]
        },
    )
    muet = _FakeHttp(payload={"items": [_item(title="T3")]})
    negatifs = _FakeHttp(
        payload={
            "items": [
                _item(
                    title="T4",
                    accessInfo={"epub": {"isAvailable": False}, "pdf": {"isAvailable": False}},
                )
            ]
        },
    )

    assert (await _client(vente).search_by_title_result("T")).items[0].has_ebook is True
    assert (await _client(acces).search_by_title_result("T")).items[0].has_ebook is True
    assert (await _client(muet).search_by_title_result("T")).items[0].has_ebook is None
    assert (await _client(negatifs).search_by_title_result("T")).items[0].has_ebook is False


@pytest.mark.asyncio
async def test_timeouts_are_not_empty_answers():
    """Timeout httpx comme `TimeoutError` nu : `timeout`, jamais `success`."""
    for erreur in (
        httpx.TimeoutException("délai dépassé"),
        TimeoutError("délai dépassé"),
    ):
        resultat = await _client(_FakeHttp(error=erreur)).search_by_title_result("T")

        assert resultat.status == ProviderQueryStatus.TIMEOUT
        assert resultat.items == ()
        assert resultat.error_code == "timeout"


@pytest.mark.asyncio
async def test_network_errors_are_unavailable():
    """Une erreur réseau n'est ni un vide ni un timeout : `unavailable`."""
    erreur = httpx.ConnectError(
        "connexion refusée",
        request=httpx.Request("GET", "https://www.googleapis.com/books/v1/volumes"),
    )

    resultat = await _client(_FakeHttp(error=erreur)).search_by_title_result("T")

    assert resultat.status == ProviderQueryStatus.UNAVAILABLE
    assert resultat.items == ()
    assert resultat.error_code == "ConnectError"


@pytest.mark.asyncio
async def test_unauthorized_and_rate_limited_statuses():
    """401/403 : `unauthorized`. 429 : `rate_limited` + `Retry-After` réel."""
    for code in (401, 403):
        resultat = await _client(_FakeHttp(status_code=code)).search_by_title_result("T")

        assert resultat.status == ProviderQueryStatus.UNAUTHORIZED
        assert resultat.items == ()
        assert resultat.error_code == f"http_{code}"
        assert resultat.retry_after_seconds is None

    quota = await _client(
        _FakeHttp(status_code=429, headers={"Retry-After": "120"})
    ).search_by_title_result("T")
    assert quota.status == ProviderQueryStatus.RATE_LIMITED
    assert quota.retry_after_seconds == 120

    quota_sans_delai = await _client(_FakeHttp(status_code=429)).search_by_title_result("T")
    assert quota_sans_delai.status == ProviderQueryStatus.RATE_LIMITED
    # Aucun délai inventé quand Google n'en renvoie pas.
    assert quota_sans_delai.retry_after_seconds is None


@pytest.mark.asyncio
async def test_server_errors_are_unavailable():
    """500/502/503/504 : `unavailable`, avec le code en `error_code`."""
    for code in (500, 502, 503, 504):
        resultat = await _client(_FakeHttp(status_code=code)).search_by_title_result("T")

        assert resultat.status == ProviderQueryStatus.UNAVAILABLE
        assert resultat.items == ()
        assert resultat.error_code == f"http_{code}"


@pytest.mark.asyncio
async def test_invalid_payloads_are_invalid_responses():
    """JSON illisible ou structure inattendue : `invalid_response`."""
    illisible = await _client(
        _FakeHttp(json_error=ValueError("JSON invalide"))
    ).search_by_title_result("T")
    assert illisible.status == ProviderQueryStatus.INVALID_RESPONSE
    assert illisible.error_code == "invalid_json"

    etrange = await _client(
        _FakeHttp(payload={"items": "pas-une-liste", "totalItems": 3})  # type: ignore[dict-item]
    ).search_by_title_result("T")
    assert etrange.status == ProviderQueryStatus.INVALID_RESPONSE
    assert etrange.error_code == "unexpected_structure"


@pytest.mark.asyncio
async def test_free_search_uses_plain_terms_with_bounded_results():
    """Le repli libre envoie titre + auteur sans opérateur, borné à 5."""
    http = _FakeHttp(payload={"totalItems": 0})
    client = GoogleBooksClient(api_key="cle-de-test", http_client=http)  # type: ignore[arg-type]

    resultat = await client.search_free_result("Blacksad", "Juan Diaz Canales")

    assert resultat.status == ProviderQueryStatus.SUCCESS
    assert http.requests[0]["params"]["q"] == "Blacksad Juan Diaz Canales"  # type: ignore[index]
    assert http.requests[0]["params"]["maxResults"] == "5"  # type: ignore[index]


@pytest.mark.asyncio
async def test_ebook_needs_explicit_availability_flags():
    """Seuls `isAvailable` booléens comptent : objet présent, aperçu ou texte ne suffisent pas."""
    sans_indicateur = _FakeHttp(
        payload={"items": [_item(title="T1", accessInfo={"epub": {}})]}
    )
    apercu_seul = _FakeHttp(
        payload={
            "items": [
                _item(
                    title="T2",
                    accessInfo={
                        "viewability": "PARTIAL",
                        "textToSpeechPermission": "ALLOWED",
                    },
                )
            ]
        },
    )
    chaine_non_booleenne = _FakeHttp(
        payload={
            "items": [_item(title="T3", accessInfo={"pdf": {"isAvailable": "yes"}})]
        },
    )
    lien_apercu = _FakeHttp(
        payload={
            "items": [
                {
                    "volumeInfo": {"title": "T4"},
                    "previewLink": "http://exemple/apercu",
                }
            ]
        },
    )

    muet = await _client(sans_indicateur).search_by_title_result("T")
    partiel = await _client(apercu_seul).search_by_title_result("T")
    texte = await _client(chaine_non_booleenne).search_by_title_result("T")
    apercu = await _client(lien_apercu).search_by_title_result("T")

    assert muet.items[0].has_ebook is None
    assert partiel.items[0].has_ebook is None
    assert texte.items[0].has_ebook is None
    assert apercu.items[0].has_ebook is None


@pytest.mark.asyncio
async def test_errors_never_leak_the_key(caplog):
    """Ni la clé ni l'URL complète dans les logs, même sur panne réseau."""
    http = _FakeHttp(error=httpx.ConnectError("panne"))
    client = GoogleBooksClient(api_key="cle-secrete", http_client=http)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="radar.google_books"):
        resultat = await client.search_by_title_result("Dailycieux")

    assert resultat.status == ProviderQueryStatus.UNAVAILABLE
    assert "cle-secrete" not in caplog.text
    assert "key=" not in caplog.text


@pytest.mark.asyncio
async def test_even_url_bearing_errors_never_leak_the_key(caplog):
    """Même si l'exception porte l'URL avec clé, seul le nom de classe est loggé."""
    cle = "CLE-FACTICE-XYZ"
    requete = httpx.Request(
        "GET", f"https://www.googleapis.com/books/v1/volumes?q=x&key={cle}"
    )
    http = _FakeHttp(error=httpx.ConnectError("panne", request=requete))

    def _avec_cle(faux_http: _FakeHttp) -> GoogleBooksClient:
        return GoogleBooksClient(api_key=cle, http_client=faux_http)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="radar.google_books"):
        delai = await _avec_cle(http).search_by_title_result("T")
        quota = await _avec_cle(_FakeHttp(status_code=429)).search_by_title_result("T")
        serveur = await _avec_cle(_FakeHttp(status_code=500)).search_by_title_result("T")

    assert delai.status == ProviderQueryStatus.UNAVAILABLE
    assert quota.status == ProviderQueryStatus.RATE_LIMITED
    assert serveur.status == ProviderQueryStatus.UNAVAILABLE
    assert cle not in caplog.text
    assert "key=" not in caplog.text
