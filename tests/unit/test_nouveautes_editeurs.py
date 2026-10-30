"""Le fournisseur des annonces de parution, sans réseau.

Ce que ces tests protègent, dans l'ordre où le fournisseur peut se tromper :
la session neuve par recherche (une session réutilisée répond à la question
précédente), le budget de requêtes, la lecture de la fiche (ISBN clé de contrôle
comprise), et surtout les deux réponses qu'il ne doit **jamais** confondre avec
une absence : une recherche non appliquée, et une fiche qui a changé de forme.
"""

from __future__ import annotations

import httpx
import pytest

from app.domain.enums import ProviderQueryStatus
from app.providers.nouveautes_editeurs import (
    MAX_HTTP_CALLS,
    NouveautesEditeursClient,
    parse_declaration,
)
from tests.fakes_nouveautes_editeurs import (
    NouveautesSite,
    SiteDeclaration,
    search_page,
    today_string,
)

ISBN = "9782812322495"
DECLARATION_ID = "10000001392716"


def declaration(**overrides) -> SiteDeclaration:
    fields = {
        "declaration_id": DECLARATION_ID,
        "title": "Dailycieux : 150 recettes pour se régaler tous les jours",
        "date": today_string(-30),
    }
    fields.update(overrides)
    return SiteDeclaration(**fields)


def client_for(site: NouveautesSite, *, timeout_seconds: float = 4.0) -> NouveautesEditeursClient:
    """Le client réel du fournisseur, branché sur le site simulé."""
    return NouveautesEditeursClient(
        timeout_seconds=timeout_seconds,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(site.respond)
        ),
    )


@pytest.mark.asyncio
async def test_isbn_search_reads_the_declaration_it_names():
    site = NouveautesSite().with_declaration(declaration(), ISBN)
    result = await client_for(site).search_by_isbn_result(ISBN)

    assert result.status == ProviderQueryStatus.SUCCESS
    (found,) = result.items
    assert found.isbn13 == ISBN
    assert found.ean13 == ISBN
    assert found.title.startswith("Dailycieux")
    assert found.author == "Whoogy's"
    assert found.publisher == "Hachette-Livre Editions du Chêne"
    assert found.collection == "Gastronomie"
    assert found.price_ttc_cents == 3500
    assert found.pages == 272
    assert found.format_detail == "272 pages ; 26,4 x 21 cm ; broché"
    assert found.legal_deposit_status == "Reçu par la BnF"
    assert found.document_type == "Livre imprimé"
    assert found.is_printed_book is True
    assert found.url.endswith(f"id_declaration={DECLARATION_ID}")


@pytest.mark.asyncio
async def test_one_search_never_exceeds_its_request_budget():
    """Amorçage, recherche, fiche : trois requêtes, et pas une de plus."""
    site = NouveautesSite().with_declaration(declaration(), ISBN)
    await client_for(site).search_by_isbn_result(ISBN)

    assert site.call_count == MAX_HTTP_CALLS
    assert site.requests[0].startswith("GET https://nouveautes-editeurs.bnf.fr/")
    assert site.requests[1].startswith("POST https://nouveautes-editeurs.bnf.fr/accueil")
    assert site.requests[2].endswith(f"id_declaration={DECLARATION_ID}")


@pytest.mark.asyncio
async def test_each_search_opens_its_own_session():
    """Le site garde la recherche en session : la partager servirait la précédente."""
    site = NouveautesSite().with_declaration(declaration(), ISBN)
    opened: list[httpx.AsyncClient] = []

    def factory() -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(site.respond))
        opened.append(client)
        return client

    provider = NouveautesEditeursClient(client_factory=factory)
    await provider.search_by_isbn_result(ISBN)
    await provider.search_by_title_result("Dailycieux")

    assert len(opened) == 2
    assert all(client.is_closed for client in opened)


@pytest.mark.asyncio
async def test_a_search_the_site_did_not_apply_is_never_an_absence():
    """Session non amorcée : 284 résultats, filtre pourtant enregistré."""
    site = NouveautesSite(applied=False)
    result = await client_for(site).search_by_isbn_result(ISBN)

    assert result.status == ProviderQueryStatus.INVALID_RESPONSE
    assert result.error_code == "unexpected_results"
    assert result.items == ()
    assert site.call_count == 2  # aucune fiche lue sur une liste non filtrée


@pytest.mark.asyncio
async def test_a_real_absence_is_a_success_without_declaration():
    site = NouveautesSite()
    result = await client_for(site).search_by_isbn_result(ISBN)

    assert result.status == ProviderQueryStatus.SUCCESS
    assert result.items == ()
    assert site.call_count == 2


@pytest.mark.asyncio
async def test_title_search_opens_the_closest_declaration():
    """Trois déclarations pour un titre : une seule fiche est lue."""
    site = NouveautesSite()
    wanted = declaration(title="Les Enfants perdus", date=today_string(-12))
    other = declaration(declaration_id="10000001406889", title="Autre chose", date=today_string(-9))
    site.with_declaration(other, "Les Enfants perdus")
    site.with_declaration(wanted, "Les Enfants perdus")

    result = await client_for(site).search_by_title_result("Les Enfants perdus")

    assert result.status == ProviderQueryStatus.SUCCESS
    assert [item.declaration_id for item in result.items] == [DECLARATION_ID]
    assert site.call_count == MAX_HTTP_CALLS


@pytest.mark.asyncio
async def test_a_changed_detail_page_is_a_named_failure():
    """La fiche ne porte plus de titre ni de date : rien n'est deviné."""
    site = NouveautesSite(broken_detail=True).with_declaration(declaration(), ISBN)
    result = await client_for(site).search_by_isbn_result(ISBN)

    assert result.status == ProviderQueryStatus.INVALID_RESPONSE
    assert result.error_code == "unexpected_declaration"
    assert result.items == ()


@pytest.mark.asyncio
async def test_a_broken_isbn_checksum_is_not_an_isbn():
    """Le code imprimé n'est retenu que s'il passe la clé : sinon `None`."""
    page = declaration(isbn="978-2-8123-2249-4", ean=None).page()
    parsed = parse_declaration(page, DECLARATION_ID)

    assert parsed is not None
    assert parsed.isbn13 is None


@pytest.mark.asyncio
async def test_an_isbn10_on_the_page_is_converted_before_use():
    """Le site n'imprime pas toujours un ISBN-13 : la conversion est faite, pas devinée."""
    page = declaration(isbn="2-8123-2249-7", ean=None).page()
    parsed = parse_declaration(page, DECLARATION_ID)

    assert parsed is not None
    assert parsed.isbn13 == ISBN


@pytest.mark.asyncio
async def test_a_detail_page_without_deposit_status_says_so_by_its_silence():
    """Un état de dépôt absent reste absent : il ne devient pas « annoncé »."""
    site = NouveautesSite()
    site.with_declaration(
        declaration(deposit=""), ISBN
    )
    result = await client_for(site).search_by_isbn_result(ISBN)

    assert result.items[0].legal_deposit_status is None


@pytest.mark.asyncio
async def test_timeout_is_reported_without_raising():
    def factory() -> httpx.AsyncClient:
        def respond(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("délai dépassé", request=request)

        return httpx.AsyncClient(transport=httpx.MockTransport(respond))

    result = await NouveautesEditeursClient(client_factory=factory).search_by_isbn_result(ISBN)

    assert result.status == ProviderQueryStatus.TIMEOUT
    assert result.error_code == "timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, ProviderQueryStatus.UNAUTHORIZED),
        (429, ProviderQueryStatus.RATE_LIMITED),
        (503, ProviderQueryStatus.UNAVAILABLE),
    ],
)
async def test_source_refusals_are_named(status, expected):
    site = NouveautesSite(status=status).with_declaration(declaration(), ISBN)
    result = await client_for(site).search_by_isbn_result(ISBN)

    assert result.status == expected
    assert result.items == ()


@pytest.mark.asyncio
async def test_an_empty_query_costs_nothing():
    """Rien à chercher : aucune requête, donc aucune session ouverte."""
    site = NouveautesSite()
    result = await client_for(site).search_by_title_result("   ")

    assert result.status == ProviderQueryStatus.INVALID_RESPONSE
    assert result.error_code == "empty_query"
    assert site.call_count == 0


def test_a_page_with_identifiers_but_no_counter_is_never_trusted():
    """Un compteur absent n'est pas un zéro : la page est illisible, pas vide."""
    page = search_page([declaration()]).replace("1 résultats", "")
    from app.providers.nouveautes_editeurs import _read_search

    search = _read_search(page)

    assert search.declaration_count == 0
    assert search.declaration_ids == (DECLARATION_ID,)
