"""L'admission d'une déclaration d'éditeur, du fournisseur jusqu'au récap.

Ces tests traversent le vrai service et le vrai cas d'usage : ce qui est simulé,
c'est le réseau — le SRU de la BNF, l'API Search d'OpenLibrary et le site des
annonces de parution. Trois choses s'y prouvent, dans cet ordre :

- la BNF d'abord, et **zéro requête** aux annonces quand elle a parlé ;
- une déclaration admise porte sa provenance et son état de dépôt, jamais une
  confirmation de catalogue ;
- toute déclaration douteuse est refusée avec son motif nommé, y compris une
  parution future, qui est une annonce et non une absence.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from app.bot.handlers import alerts_helpers
from app.core.config import settings
from app.domain.enums import BookSearchStatus
from app.providers.nouveautes_editeurs import NouveautesEditeursClient
from app.services.book_metadata_service import BookMetadataService
from app.use_cases.book_search import BookSearchUseCase, InvalidIsbnError
from tests.fakes_catalogue import BNF_RECORD_TEMPLATE, SRU_HEADER
from tests.fakes_nouveautes_editeurs import (
    NouveautesSite,
    SiteDeclaration,
    today_string,
)

ISBN = "9782812322495"
TITLE = "Dailycieux"
AUTHOR = "Whoogy's"


def bnf_record(*, isbn: str = ISBN, title: str = TITLE, creator: str = AUTHOR) -> str:
    return BNF_RECORD_TEMPLATE.format(
        ark="471036825",
        title=title,
        creator=creator,
        publisher="Hachette-Livre Editions du Chêne",
        date="<dc:date>2026</dc:date>",
        fmt="1 vol. (272 p.) ; 26 cm",
        isbn=isbn,
    )


def declaration(**overrides) -> SiteDeclaration:
    fields = {
        "declaration_id": "10000001392716",
        "title": f"{TITLE} : 150 recettes pour se régaler tous les jours",
        "date": today_string(-20),
        "author": AUTHOR,
    }
    fields.update(overrides)
    return SiteDeclaration(**fields)


class Web:
    """Les quatre sources d'un parcours d'ajout, simulées ensemble.

    Le site des annonces est une **frontière surveillée** : `announcements` compte
    ses requêtes, et un test qui n'en attend aucune le prouve en le laissant à zéro
    plutôt qu'en espérant un ordre d'appels.
    """

    def __init__(
        self,
        *,
        bnf_records: tuple[str, ...] = (),
        ol_docs: tuple[dict, ...] = (),
        site: NouveautesSite | None = None,
    ) -> None:
        self.bnf_records = bnf_records
        self.ol_docs = ol_docs
        self.site = site or NouveautesSite()
        self.hosts: list[str] = []

    def respond(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.hosts.append(host)
        if host == "catalogue.bnf.fr":
            payload = SRU_HEADER.format(
                count=len(self.bnf_records), records="".join(self.bnf_records)
            )
            return httpx.Response(200, content=payload.encode())
        if host == "openlibrary.org":
            return httpx.Response(
                200, json={"numFound": len(self.ol_docs), "docs": list(self.ol_docs)}
            )
        if host == "nouveautes-editeurs.bnf.fr":
            return self.site.respond(request)
        raise AssertionError(f"Source inattendue : {host}")

    @property
    def announcements(self) -> int:
        return self.site.call_count

    def service(self) -> BookMetadataService:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        return BookMetadataService(
            http_client=client,
            declared_source_client=NouveautesEditeursClient(
                client_factory=lambda: httpx.AsyncClient(
                    transport=httpx.MockTransport(self.site.respond)
                )
            ),
        )


@pytest.fixture(autouse=True)
def no_google_key(monkeypatch):
    """Google Books muet : la troisième autorité ne brouille pas les comptes."""
    monkeypatch.setattr(settings, "GOOGLE_BOOKS_API_KEY", SecretStr(""))


@pytest.mark.asyncio
async def test_bnf_confirming_the_isbn_never_asks_the_announcements():
    """La BNF a parlé : la source déclarée n'est même pas construite."""
    web = Web(bnf_records=(bnf_record(),))
    result = await BookSearchUseCase(web.service()).search_by_isbn(ISBN)

    assert result.status == BookSearchStatus.FOUND
    assert result.provenance == "bnf"
    assert web.announcements == 0
    assert "nouveautes-editeurs.bnf.fr" not in web.hosts


@pytest.mark.asyncio
async def test_bnf_confirming_the_title_never_asks_the_announcements():
    web = Web(bnf_records=(bnf_record(),))
    result = await BookSearchUseCase(web.service()).search_by_title(TITLE, AUTHOR)

    assert result.status == BookSearchStatus.FOUND
    assert result.provenance == "bnf"
    assert web.announcements == 0


@pytest.mark.asyncio
async def test_an_isbn_the_catalogue_ignores_is_named_by_its_declaration():
    """Le cas du lot : paru, déclaré, pas encore catalogué."""
    web = Web()
    web.site.with_declaration(declaration(), ISBN)
    result = await BookSearchUseCase(web.service()).search_by_isbn(ISBN)

    assert result.status == BookSearchStatus.FOUND
    assert result.provenance == "nouveautes_editeurs"
    assert result.confirmed.author == AUTHOR
    assert result.confirmed.publisher == "Hachette-Livre Editions du Chêne"
    assert result.consulted == ("bnf", "openlibrary", "nouveautes_editeurs")
    assert web.announcements <= 3


@pytest.mark.asyncio
async def test_the_declared_identity_is_never_presented_as_catalogue_confirmed():
    """Provenance et dépôt légal visibles, et pas un mot de confirmation BnF."""
    web = Web()
    web.site.with_declaration(declaration(), ISBN)
    result = await BookSearchUseCase(web.service()).search_by_isbn(ISBN)

    note = alerts_helpers._isbn_verification_note(result)

    assert "Nouveautés Éditeurs" in note
    assert "dépôt légal : Reçu par la BnF" in note
    assert "non confirmée par le catalogue" in note
    assert result.confirmed.title.startswith(TITLE)


@pytest.mark.asyncio
async def test_a_title_the_catalogue_misses_is_resolved_by_its_declaration():
    web = Web()
    web.site.with_declaration(declaration(title=TITLE), TITLE)
    result = await BookSearchUseCase(web.service()).search_by_title(TITLE, AUTHOR)

    assert result.status == BookSearchStatus.FOUND
    assert result.provenance == "nouveautes_editeurs"
    assert result.confirmed.isbn13 == ISBN
    assert result.consulted == ("bnf", "openlibrary", "nouveautes_editeurs")


@pytest.mark.asyncio
async def test_a_declaration_by_another_author_is_refused_by_name():
    web = Web()
    web.site.with_declaration(declaration(title=TITLE, author="Quelqu'un d'autre"), TITLE)
    result = await BookSearchUseCase(web.service()).search_by_title(TITLE, AUTHOR)

    assert result.status == BookSearchStatus.NOT_FOUND
    assert result.confirmed is None
    assert result.declared_refusal is not None
    assert "Quelqu'un d'autre" in result.declared_refusal
    assert AUTHOR in result.declared_refusal


@pytest.mark.asyncio
async def test_a_publication_still_announced_is_refused_as_announced():
    """Parution future : une annonce, pas une absence de catalogue."""
    web = Web()
    future = today_string(+30)
    web.site.with_declaration(declaration(date=future), ISBN)
    result = await BookSearchUseCase(web.service()).search_by_isbn(ISBN)

    assert result.status == BookSearchStatus.NOT_FOUND
    assert result.declared_refusal is not None
    assert "parution annoncée" in result.declared_refusal
    assert "pas encore parue" in result.declared_refusal
    assert future in result.declared_refusal
    note = alerts_helpers._isbn_verification_note(result)
    assert "parution annoncée" in note


@pytest.mark.asyncio
async def test_another_isbn_on_the_declaration_is_refused_before_anything_else():
    web = Web()
    web.site.with_declaration(
        declaration(isbn="978-2-264-06989-4", ean="9782264069894"), ISBN
    )
    result = await BookSearchUseCase(web.service()).search_by_isbn(ISBN)

    assert result.status == BookSearchStatus.NOT_FOUND
    assert result.declared_refusal is not None
    assert "9782264069894" in result.declared_refusal
    assert ISBN in result.declared_refusal


@pytest.mark.asyncio
async def test_a_malformed_isbn_costs_no_request_at_all():
    """Un code qui ne passe pas la clé est refusé avant tout appel réseau."""
    web = Web()
    with pytest.raises(InvalidIsbnError):
        await BookSearchUseCase(web.service()).search_by_isbn("9782812322494")

    assert web.hosts == []
    assert web.announcements == 0


@pytest.mark.asyncio
async def test_a_declaration_without_isbn_is_refused():
    web = Web()
    web.site.with_declaration(declaration(isbn=None, ean=None), ISBN)
    result = await BookSearchUseCase(web.service()).search_by_isbn(ISBN)

    assert result.status == BookSearchStatus.NOT_FOUND
    assert result.declared_refusal is not None
    assert "ISBN-13 valide" in result.declared_refusal


@pytest.mark.asyncio
async def test_a_declaration_that_is_not_a_printed_book_is_refused():
    web = Web()
    web.site.with_declaration(declaration(document_type="Partition"), ISBN)
    result = await BookSearchUseCase(web.service()).search_by_isbn(ISBN)

    assert result.status == BookSearchStatus.NOT_FOUND
    assert result.declared_refusal is not None
    assert "Partition" in result.declared_refusal


@pytest.mark.asyncio
async def test_a_publisher_divergence_is_refused_when_the_user_named_one():
    """L'éditeur fourni par l'usager est un critère de refus, pas un terme de requête."""
    web = Web()
    web.site.with_declaration(
        declaration(title=TITLE, publisher="Autre maison d'édition"), TITLE
    )
    result = await BookSearchUseCase(web.service()).search_by_title(
        TITLE, AUTHOR, publisher_hint="Gallimard"
    )

    assert result.status == BookSearchStatus.NOT_FOUND
    assert result.declared_refusal is not None
    assert "Autre maison d'édition" in result.declared_refusal
    assert "Gallimard" in result.declared_refusal


@pytest.mark.asyncio
async def test_a_publisher_the_user_named_does_not_block_a_matching_declaration():
    """« Editions du Chêne » est contenu dans la raison sociale complète du site.

    Un nom commercial plus court que la raison sociale (« Le Chêne ») reste, lui,
    refusé : la comparaison ne devine pas les alias d'éditeur.
    """
    web = Web()
    web.site.with_declaration(declaration(title=TITLE), TITLE)
    result = await BookSearchUseCase(web.service()).search_by_title(
        TITLE, AUTHOR, publisher_hint="Editions du Chêne"
    )

    assert result.status == BookSearchStatus.FOUND
    assert result.provenance == "nouveautes_editeurs"


@pytest.mark.asyncio
async def test_the_announcement_source_being_down_keeps_todays_refusal():
    """Une panne de la source déclarée n'est pas un refus nouveau, ni une panne BNF."""
    site = NouveautesSite(status=503).with_declaration(declaration(), ISBN)
    web = Web(site=site)
    result = await BookSearchUseCase(web.service()).search_by_isbn(ISBN)

    assert result.status == BookSearchStatus.NOT_FOUND
    assert result.declared_refusal is None
    # L'échec reste nommé, et il ne transforme pas l'absence en panne d'autorité.
    assert "nouveautes_editeurs" in result.unavailable
    note = alerts_helpers._isbn_verification_note(result)
    assert "Sources indisponibles : Nouveautés Éditeurs (BnF)" in note
    assert "vérification incomplète, réessaie plus tard" not in note


@pytest.mark.asyncio
async def test_an_unreadable_announcement_page_is_a_failure_not_an_absence():
    site = NouveautesSite(applied=False).with_declaration(declaration(), ISBN)
    web = Web(site=site)
    result = await BookSearchUseCase(web.service()).search_by_isbn(ISBN)

    assert result.status == BookSearchStatus.NOT_FOUND
    assert result.declared_refusal is None
    assert result.confirmed is None


@pytest.mark.asyncio
async def test_the_add_card_carries_the_refusal_reason():
    """Le brouillon dit pourquoi la déclaration a été écartée."""
    web = Web()
    web.site.with_declaration(declaration(date=today_string(+10)), ISBN)
    result = await BookSearchUseCase(web.service()).search_by_isbn(ISBN)

    updates = alerts_helpers.draft_updates_from_search(result, query_title=TITLE)

    assert updates["isbn"] is None
    assert updates["isbn_note"] is not None
    assert "parution annoncée" in updates["isbn_note"]

