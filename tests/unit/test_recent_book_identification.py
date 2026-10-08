"""Nouveautés : homonymes, replis et bilan réel du brouillon, sans réseau."""

from html import escape

import httpx
import pytest
from pydantic import SecretStr

from app.bot.handlers import alerts_helpers
from app.core.config import settings
from app.domain.enums import BookSearchStatus
from app.services.book_metadata_service import BookMetadataService
from app.use_cases.book_search import BookSearchUseCase
from tests.fakes_catalogue import BNF_RECORD_TEMPLATE, SRU_HEADER

ISBN = "9782810206087"
HOMONYM_ISBN = "9782214080627"


def _record(author: str, isbn: str) -> str:
    return BNF_RECORD_TEMPLATE.format(
        ark="35045569f",
        title="Les Enfants perdus",
        creator=author,
        publisher="Éditeur",
        date="<dc:date>2026</dc:date>",
        fmt="200 p.",
        isbn=isbn,
    )


def _client(
    *,
    records=(),
    ol_author=None,
    ol_status=200,
    bnf_status=200,
    google_author=None,
    google_authors=None,
):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "catalogue.bnf.fr":
            return httpx.Response(
                bnf_status,
                content=SRU_HEADER.format(count=len(records), records="".join(records)).encode(),
            )
        if request.url.host == "openlibrary.org":
            docs = (
                []
                if ol_author is None
                else [
                    {
                        "title": "Les Enfants perdus",
                        "author_name": [ol_author],
                        "isbn": [ISBN],
                        "publisher": ["Rue de Sèvres"],
                        "first_publish_year": 2026,
                    }
                ]
            )
            return httpx.Response(ol_status, json={"numFound": len(docs), "docs": docs})
        if request.url.host == "www.googleapis.com":
            items = (
                []
                if google_author is None
                else [
                    {
                        "volumeInfo": {
                            "title": "Les Enfants perdus",
                            "authors": [google_author],
                            "industryIdentifiers": [{"type": "ISBN_13", "identifier": ISBN}],
                        }
                    }
                ]
            )
            if google_authors is not None:
                items = [
                    {
                        "volumeInfo": {
                            "title": "Les Enfants perdus",
                            "authors": [author],
                            "industryIdentifiers": [{"type": "ISBN_13", "identifier": code}],
                        }
                    }
                    for author, code in google_authors
                ]
            return httpx.Response(200, json={"totalItems": len(items), "items": items})
        raise AssertionError(f"Source inattendue : {request.url.host}")

    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


@pytest.fixture(autouse=True)
def no_google_key(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_BOOKS_API_KEY", SecretStr(""))


@pytest.mark.asyncio
async def test_explicit_author_continues_after_bnf_homonyms():
    async with _client(
        records=[_record("Rosemonde Ahou de Saintange", HOMONYM_ISBN)], ol_author="Neyef"
    ) as client:
        service = BookMetadataService(http_client=client)
        report = await service.resolve_for_alert_report("Les Enfants perdus", "Neyef")
        result = await BookSearchUseCase(service).search_by_title("Les Enfants perdus", "Neyef")
    assert result.status == BookSearchStatus.FOUND
    assert result.confirmed.author == "Neyef"
    assert result.reference.isbn13 == ISBN
    assert result.consulted == ("bnf", "openlibrary")
    assert result.provenance == "openlibrary"
    assert report.resolution.other_works[0].isbn13 == HOMONYM_ISBN
    assert [e.isbn13 for e in result.editions] == [ISBN]


@pytest.mark.asyncio
async def test_wrong_author_in_first_fallback_does_not_hide_next_source(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_BOOKS_API_KEY", SecretStr("dummy-test-key"))
    async with _client(
        records=[_record("Rosemonde Ahou de Saintange", HOMONYM_ISBN)],
        ol_author="Autre auteur",
        google_author="Neyef",
    ) as client:
        result = await BookSearchUseCase(BookMetadataService(http_client=client)).search_by_title(
            "Les Enfants perdus", "Neyef"
        )
    assert result.status == BookSearchStatus.FOUND
    assert result.confirmed.author == "Neyef"
    assert result.provenance == "google_books"
    assert result.consulted == ("bnf", "openlibrary", "google_books")


@pytest.mark.asyncio
async def test_failed_fallback_keeps_bnf_homonyms_and_real_attempts():
    async with _client(
        records=[_record("Rosemonde Ahou de Saintange", HOMONYM_ISBN)], ol_status=503
    ) as client:
        result = await BookSearchUseCase(BookMetadataService(http_client=client)).search_by_title(
            "Les Enfants perdus", "Neyef"
        )
    assert result.status == BookSearchStatus.AMBIGUOUS
    assert result.reference is None
    assert result.works[0].isbn13 == HOMONYM_ISBN
    assert result.consulted == ("bnf", "openlibrary")
    assert result.unavailable == ("openlibrary",)


@pytest.mark.asyncio
@pytest.mark.parametrize("author", [None, "Rosemonde Ahou de Saintange"])
async def test_bnf_answer_without_author_conflict_does_not_search_fallbacks(author):
    async with _client(
        records=[_record("Rosemonde Ahou de Saintange", HOMONYM_ISBN)], ol_author="Neyef"
    ) as client:
        result = await BookSearchUseCase(BookMetadataService(http_client=client)).search_by_title(
            "Les Enfants perdus", author
        )
    assert result.consulted == ("bnf",)
    assert result.reference.isbn13 == HOMONYM_ISBN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bnf_status", "ol_status", "wanted_status"),
    [
        (200, 200, "not_found"),
        (200, 503, "unavailable"),
        (503, 200, "partial"),
    ],
)
async def test_typed_isbn_card_reports_real_sources(
    monkeypatch, bnf_status, ol_status, wanted_status
):
    async with _client(
        bnf_status=bnf_status, ol_status=ol_status, ol_author="Neyef" if bnf_status == 503 else None
    ) as client:
        service = BookMetadataService(http_client=client)
        monkeypatch.setattr(alerts_helpers, "BookSearchUseCase", lambda: BookSearchUseCase(service))
        alignment = await alerts_helpers.realign_draft_to_isbn(
            {"name": "Les Enfants perdus", "author": "Ancien auteur"}, ISBN
        )
    updates = alignment.updates
    card = alerts_helpers.creation_card({"name": "Les Enfants perdus", **updates})
    assert updates["isbn"] == ISBN  # La saisie volontaire garde son contrat de persistance.
    assert updates["author"] is None
    assert updates["publisher"] is None
    assert updates["book_context"] is None
    assert "Auteur :</b> Non renseigné" in card
    assert "Éditeur :</b> Non renseigné" in card
    assert "Sources consultées : BNF, OpenLibrary" in card
    assert "Google Books" not in card
    if wanted_status == "not_found":
        assert "aucune notice exploitable" in card
        assert "indisponible" not in card
    elif wanted_status == "unavailable":
        assert "Sources indisponibles : OpenLibrary" in card
        assert "la BNF n'a pas pu" not in card
    else:
        assert "Candidat" in card
        assert "Neyef" in card
        assert "Sources indisponibles : BNF" in card
        assert "aucune source ne catalogue" not in card


@pytest.mark.asyncio
async def test_isbn_contradiction_never_uses_a_fallback(monkeypatch):
    different_book = _record("Autre auteur", ISBN).replace(
        "Les Enfants perdus", escape("Un tout autre ouvrage")
    )
    async with _client(records=[different_book], ol_author="Neyef") as client:
        service = BookMetadataService(http_client=client)
        result = await BookSearchUseCase(service).search_by_isbn(ISBN, "Les Enfants perdus")
        monkeypatch.setattr(alerts_helpers, "BookSearchUseCase", lambda: BookSearchUseCase(service))
        alignment = await alerts_helpers.realign_draft_to_isbn({"name": "Les Enfants perdus"}, ISBN)
    assert result.consulted == ("bnf",)
    assert alignment.updates == {}
    assert "Un tout autre ouvrage" in alignment.conflict


@pytest.mark.asyncio
async def test_title_fallback_cannot_reassign_an_isbn_bnf_attaches_to_another_author():
    async with _client(
        records=[_record("Rosemonde Ahou de Saintange", ISBN)], ol_author="Neyef"
    ) as client:
        result = await BookSearchUseCase(BookMetadataService(http_client=client)).search_by_title(
            "Les Enfants perdus", "Neyef"
        )
    assert result.status == BookSearchStatus.AMBIGUOUS
    assert result.reference is None
    assert result.works[0].author == "Rosemonde Ahou de Saintange"


@pytest.mark.asyncio
async def test_unexpected_failure_is_not_reported_as_catalogue_absence(monkeypatch, caplog):
    class FailingSearch:
        async def search_by_isbn(self, *_args, **_kwargs):
            raise RuntimeError("secret=must-not-appear")

    monkeypatch.setattr(alerts_helpers, "BookSearchUseCase", FailingSearch)
    alignment = await alerts_helpers.realign_draft_to_isbn({"name": "Les Enfants perdus"}, ISBN)
    note = alignment.updates["isbn_note"]
    assert "vérification impossible" in note
    assert "aucune notice" not in note
    assert "Sources consultées" not in note
    assert "must-not-appear" not in caplog.text


@pytest.mark.asyncio
async def test_google_fallback_selects_matching_author_among_homonyms(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_BOOKS_API_KEY", SecretStr("dummy-test-key"))
    async with _client(
        records=[_record("Rosemonde Ahou de Saintange", HOMONYM_ISBN)],
        google_authors=[("Rosemonde Ahou de Saintange", HOMONYM_ISBN), ("Neyef", ISBN)],
    ) as client:
        result = await BookSearchUseCase(BookMetadataService(http_client=client)).search_by_title(
            "Les Enfants perdus", "Neyef"
        )
    assert result.status == BookSearchStatus.FOUND
    assert result.confirmed.author == "Neyef"
    assert result.reference.isbn13 == ISBN
    assert result.provenance == "google_books"
