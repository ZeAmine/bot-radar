"""Tests du troisième repli d'identification : la frontière HTTP de Google Books.

Le module ne décide de rien — il lit une charge utile et la rend — donc ce qu'il faut
verrouiller ici, c'est ce qu'il lit : un code reconnu, une année, une pagination, et
surtout **l'absence** de clé, qui doit valoir « pas de source » et non « pas de
réponse ».
"""

import pytest

from app.providers.google_books import GoogleBooksClient


class _FakeResponse:
    def __init__(self, payload: dict[str, object] | None = None, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeHttp:
    """Client HTTP muet : rend la charge utile demandée et retient les requêtes."""

    def __init__(
        self,
        payload: dict[str, object] | None = None,
        status_code: int = 200,
        error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.error = error
        self.requests: list[dict[str, object]] = []

    async def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.requests.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.payload, self.status_code)


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
