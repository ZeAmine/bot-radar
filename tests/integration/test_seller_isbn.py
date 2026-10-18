"""L'ISBN vendeur n'est pas une identité : API, envoi et confirmation réels."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.dependencies import get_db
from app.api.routes.ingest import router
from app.core import tasks
from app.core.config import settings
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.category import Category
from app.db.models.listing import Listing
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.domain.enums import AlertStatus
from app.schemas.listing import ListingIngestPayload
from app.services.book_metadata_service import (
    BibliographicReport,
    BookMetadata,
    BookMetadataService,
    IsbnVerdict,
)
from app.use_cases import ingest_listing as ingestion
from app.use_cases import link_bd_catalog as catalog_linking
from app.use_cases import listing_book_confirmation as confirmation
from tests.database import postgres_replayed_database
from tests.telegram_fakes import acknowledged_delivery

ISBN = "9782369903062"


def payload(code=ISBN, title="Cible BD", external_id="1"):
    return {
        "source_name": "synthetic",
        "external_id": external_id,
        "url": "https://example.invalid/item",
        "title": title,
        "category_slug": "bd_comics",
        "item_price_cents": 100,
        "isbn13": code,
    }


@pytest.fixture
async def journey(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [601, 602])
    async with postgres_replayed_database() as db:
        monkeypatch.setattr(tasks, "_background_tasks", set())
        monkeypatch.setattr(tasks, "_cleanup_tasks", set())
        monkeypatch.setattr("app.db.session.AsyncSessionLocal", db.maker)
        monkeypatch.setattr(confirmation, "AsyncSessionLocal", db.maker)
        monkeypatch.setattr(settings, "AI_ENABLED", False)
        category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        user = User(telegram_user_id=601, is_allowed=True)
        db.session.add_all([category, user])
        await db.session.flush()
        alert = Alert(
            user_id=user.id,
            category_id=category.id,
            name="Cible BD",
            include_keywords=["cible"],
            exclude_keywords=[],
            variants=[],
            max_item_price_cents=5000,
            status=AlertStatus.ACTIVE,
        )
        db.session.add(alert)
        await db.session.commit()
        order, priced, captions = [], [], []

        async def send(**kwargs):
            order.append("telegram")
            captions.append(kwargs["market_comparison_line"])
            return acknowledged_delivery(**kwargs)

        notice = BookMetadata(title="Cible BD", author="Auteur A", publisher="Éditeur", isbn13=ISBN)
        report = BibliographicReport(
            kind="isbn", verdict=IsbnVerdict(outcome="confirme", notice=notice, consulted=("bnf",))
        )

        async def authorities(_code):
            order.append("authority")
            return report

        consult = AsyncMock(side_effect=authorities)
        monkeypatch.setattr(BookMetadataService, "consult_authorities_report", consult)
        fallback = AsyncMock(return_value=False)
        monkeypatch.setattr(catalog_linking, "link_books_from_bnf", fallback)

        def price(book_id, **_kwargs):
            priced.append(book_id)

        monkeypatch.setattr("app.services.book_market_pricing.schedule_book_pricing", price)
        monkeypatch.setattr(catalog_linking, "schedule_book_pricing", price)
        monkeypatch.setattr("app.services.book_market_pricing.schedule_book_pricing", price)

        context = SimpleNamespace(
            db=db,
            alert=alert,
            report=report,
            consult=consult,
            fallback=fallback,
            order=order,
            priced=priced,
            captions=captions,
            sender=send,
        )

        async def ingest(raw):
            async with db.maker() as session:
                return await ingestion.ProcessListingUseCase(
                    session, SimpleNamespace(id=123456789), sender=context.sender
                ).execute(ListingIngestPayload(**raw), raw)

        context.ingest = ingest
        yield context
        await tasks.wait_all_background_tasks(timeout=0)


async def books_and_links(journey):
    async with journey.db.maker() as session:
        return (
            (await session.execute(select(Book))).scalars().all(),
            (await session.execute(select(AlertBook))).scalars().all(),
        )


@pytest.mark.parametrize("code", ["invalide", "9782369903063", "X306406152", "4006381333931"])
async def test_invalid_isbn_api_keeps_listing_and_warns(journey, caplog, code, monkeypatch):
    monkeypatch.setattr("app.api.routes.ingest.send_listing_alert", journey.sender)
    app = FastAPI()
    app.include_router(router)

    async def database():
        async with journey.db.maker() as session:
            yield session

    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/ingest/listings",
            json=payload(code),
            headers={"Authorization": f"Bearer {settings.INGEST_API_TOKEN.get_secret_value()}"},
        )
    assert response.status_code == 200
    assert response.json()["warnings"] == ["listing_isbn_invalid_ignored"]
    await tasks.wait_all_background_tasks()
    assert journey.order == ["telegram"]
    journey.consult.assert_not_awaited()
    journey.fallback.assert_awaited_once()
    assert await books_and_links(journey) == ([], [])
    async with journey.db.maker() as session:
        listing = (await session.execute(select(Listing))).scalar_one()
        assert listing.isbn13 is None
        assert listing.raw_payload["isbn13"] == code
    assert any(getattr(r, "event", None) == "listing_isbn_invalid_ignored" for r in caplog.records)


@pytest.mark.parametrize(
    ("code", "canonical"),
    [("0306406152", "9780306406157"), ("978-2-36990-306-2", ISBN), (ISBN, ISBN)],
)
async def test_normalized_seller_code_is_not_confirmation(journey, code, canonical):
    journey.consult.return_value = journey.report
    result = await journey.ingest(payload(code))
    assert result.warnings == []
    async with journey.db.maker() as session:
        assert (await session.execute(select(Listing))).scalar_one().isbn13 == canonical
    await tasks.wait_all_background_tasks()


async def test_confirmation_follows_all_deliveries_and_uses_notice(journey):
    async with journey.db.maker() as session:
        source = await AlertRepository(session).get_by_id(journey.alert.id)
        second = Alert(
            user_id=source.user_id,
            category_id=source.category_id,
            name=source.name,
            include_keywords=["cible"],
            exclude_keywords=[],
            variants=[],
            max_item_price_cents=5000,
            status=AlertStatus.ACTIVE,
        )
        session.add(second)
        await session.commit()
    await journey.ingest(payload(title="Cible BD neuf"))
    await tasks.wait_all_background_tasks()
    assert journey.order[:2] == ["telegram", "telegram"]
    books, links = await books_and_links(journey)
    assert len(books) == 1
    assert len(links) == 2
    assert (books[0].title, books[0].author, books[0].publisher) == (
        "Cible BD",
        "Auteur A",
        "Éditeur",
    )
    assert journey.priced == []


@pytest.mark.parametrize(
    ("outcome", "notice_title"),
    [("confirme", "Autre ouvrage"), ("silence", "Cible BD"), ("inconnu", None)],
)
async def test_unconfirmed_or_foreign_code_never_links_or_prices(
    journey, outcome, notice_title, caplog
):
    caplog.set_level("INFO", logger="radar.use_cases.ingest_listing")
    notice = (
        BookMetadata(title=notice_title, author="Auteur A", isbn13=ISBN) if notice_title else None
    )
    report = BibliographicReport(
        kind="isbn",
        verdict=IsbnVerdict(
            outcome=outcome,
            notice=notice,
            consulted=("bnf",),
            panne="synthetic_unavailable" if outcome == "silence" else None,
        ),
    )
    journey.consult.side_effect = None
    journey.consult.return_value = report
    await journey.ingest(payload())
    await tasks.wait_all_background_tasks()
    assert await books_and_links(journey) == ([], [])
    assert journey.priced == []

    record = next(
        r for r in caplog.records if getattr(r, "event", None) == "listing_isbn_unconfirmed"
    )
    assert record.status == ("partial" if outcome == "silence" else "not_found")


async def test_existing_foreign_book_is_not_a_reference(journey):
    async with journey.db.maker() as session:
        session.add(
            Book(
                title="Autre ouvrage",
                normalized_title="autre ouvrage",
                author="Auteur B",
                isbn13=ISBN,
            )
        )
        await session.commit()
    journey.report.verdict.notice.title = "Autre ouvrage"
    await journey.ingest(payload())
    await tasks.wait_all_background_tasks()
    books, links = await books_and_links(journey)
    assert links == []
    assert journey.priced == []
    assert (books[0].title, books[0].author) == ("Autre ouvrage", "Auteur B")


@pytest.mark.parametrize("change", ["name", "isbn", "pause", "delete", "preparation"])
async def test_late_confirmation_cannot_link_after_user_change(journey, change):
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(_code):
        entered.set()
        await release.wait()
        return journey.report

    journey.consult.side_effect = held
    await journey.ingest(payload())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with journey.db.maker() as session:
            repo = AlertRepository(session)
            alert = await repo.get_by_id(journey.alert.id)
            assert await repo.lock_for_edit(alert)
            if change == "name":
                alert.name = "Nouvelle identité"
            elif change == "isbn":
                alert.isbn = "9780306406157"
            elif change == "pause":
                alert.status = AlertStatus.PAUSED
            elif change == "preparation":
                from app.domain.entities.alert_preparation import PreparationRequest

                alert.preparation = PreparationRequest(author_hint="Auteur modifié").stored()
            else:
                alert.status = AlertStatus.DELETED
            await session.commit()
    finally:
        release.set()
        await tasks.wait_all_background_tasks()
    assert await books_and_links(journey) == ([], [])
    assert journey.priced == []


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="Verrou PostgreSQL réel requis")
async def test_concurrent_confirmations_same_alert_are_idempotent(journey):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def held(_code):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
        await release.wait()
        return journey.report

    journey.consult.side_effect = held
    await journey.ingest(payload(external_id="1"))
    await journey.ingest(payload(external_id="2"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        release.set()
        await tasks.wait_all_background_tasks()
    books, links = await books_and_links(journey)
    assert len(books) == len(links) == 1


@pytest.mark.parametrize(
    ("listing_title", "notice_title"),
    [
        ("Lot Cible BD", "Cible BD"),
        ("Cible BD Tome 2", "Cible BD Tome 1"),
    ],
)
async def test_notice_cannot_turn_a_lot_or_wrong_tome_into_one_book(
    journey, listing_title, notice_title
):
    journey.report.verdict.notice.title = notice_title
    await journey.ingest(payload(title=listing_title))
    await tasks.wait_all_background_tasks()
    assert await books_and_links(journey) == ([], [])
    assert journey.priced == []


@pytest.mark.parametrize("suffix", [" Tome 2", ""])
async def test_existing_book_wrong_tome_is_not_repaired_or_linked(journey, suffix):
    # Long titre commun : la similarité seule reste supérieure au seuil existant.
    name = "Les aventures extraordinaires du personnage Cible BD"
    async with journey.db.maker() as session:
        alert = await AlertRepository(session).get_by_id(journey.alert.id)
        alert.name = name
        session.add(
            Book(
                title=name + " Tome 1",
                normalized_title=name.lower() + " tome 1",
                isbn13=ISBN,
                author="Auteur A",
            )
        )
        await session.commit()
    journey.report.verdict.notice.title = name + " Tome 2"
    await journey.ingest(payload(title=name + suffix))
    await tasks.wait_all_background_tasks()
    books, links = await books_and_links(journey)
    assert books[0].title == name + " Tome 1"
    assert links == []
    assert journey.priced == []


async def test_notice_wrong_tome_for_alert_is_not_linked(journey):
    async with journey.db.maker() as session:
        alert = await AlertRepository(session).get_by_id(journey.alert.id)
        alert.name = "Cible BD Tome 1"
        await session.commit()
    journey.report.verdict.notice.title = "Cible BD Tome 2"
    await journey.ingest(payload(title="Cible BD Tome 2"))
    await tasks.wait_all_background_tasks()
    assert await books_and_links(journey) == ([], [])
    assert journey.priced == []


async def test_existing_coherent_book_metadata_is_preserved(journey):
    async with journey.db.maker() as session:
        session.add(
            Book(
                title="Cible BD",
                normalized_title="cible bd",
                author="Auteur A",
                publisher="Éditeur conservé",
                isbn13=ISBN,
            )
        )
        await session.commit()
    await journey.ingest(payload())
    assert "Prix bas" not in journey.captions[0]
    await tasks.wait_all_background_tasks()
    books, links = await books_and_links(journey)
    assert len(links) == 1
    assert books[0].publisher == "Éditeur conservé"


async def test_legacy_invalid_isbn_is_ignored_without_rewriting_listing(journey, monkeypatch):
    original = journey.sender
    from datetime import UTC, datetime, timedelta

    from app.db.models.listing_match import ListingMatch
    from app.services.telegram_service import DeliveryResult

    monkeypatch.setattr(
        journey,
        "sender",
        AsyncMock(
            return_value=DeliveryResult("rejected", "text", reason="api_rejected", retry_after=1)
        ),
    )
    assert (await journey.ingest(payload(None))).needs_retry
    await tasks.wait_all_background_tasks()
    async with journey.db.maker() as session:
        listing = (await session.execute(select(Listing))).scalar_one()
        listing.isbn13 = "invalide"
        row = (await session.scalars(select(ListingMatch))).one()
        assert row.notification_state == "ready"
        assert row.notified_at is None
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    monkeypatch.setattr(journey, "sender", original)
    result = await journey.ingest(payload(None))
    await tasks.wait_all_background_tasks()
    assert result.warnings == ["listing_isbn_invalid_ignored"]
    assert result.matches[0].notification_sent
    async with journey.db.maker() as session:
        assert (await session.execute(select(Listing))).scalar_one().isbn13 == "invalide"
    journey.consult.assert_not_awaited()
