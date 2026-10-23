"""Mutations réelles et recalcul central : aucune somme de l'ancien ensemble."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm.attributes import NO_VALUE

from app.core import tasks
from app.core.config import settings
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.domain.enums import AlertStatus, ComparisonStatus
from app.providers.base import CollectionAttempt, MarketOffers
from app.schemas.listing import ListingIngestPayload
from app.services.book_metadata_service import BookMetadata, BookMetadataService, CatalogueVerdict
from app.services.market_price_service import MarketPriceService
from app.use_cases import ingest_listing as ingestion
from app.use_cases import link_bd_catalog as catalog_linking
from app.use_cases.view_market_details import ViewMarketDetailsUseCase
from tests.database import postgres_replayed_database
from tests.telegram_fakes import telegram_message


@pytest.fixture
async def journey(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [601, 602])
    async with postgres_replayed_database() as db:
        db.maker.configure(autoflush=False)
        monkeypatch.setattr("app.db.session.AsyncSessionLocal", db.maker)
        c = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        u = User(telegram_user_id=601, is_allowed=True)
        db.session.add_all([c, u])
        await db.session.flush()
        alert = Alert(
            user_id=u.id,
            category_id=c.id,
            name="Cible BD",
            include_keywords=["cible"],
            status=AlertStatus.ACTIVE,
            series_total_cents=3000,
        )
        books = []
        for n, code in enumerate(("9782369903086", "9782369904151", "9782369903062"), 1):
            value = Book(
                title=f"Cible BD Tome {n}", normalized_title=f"cible bd tome {n}", isbn13=code
            )
            value.market_cache = BookMarketCache(
                lowest_delivered_price_cents=n * 1000,
                comparison_status=ComparisonStatus.AVAILABLE,
                checked_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
            books.append(value)
        db.session.add_all([alert, *books])
        await db.session.flush()
        db.session.add_all([AlertBook(alert_id=alert.id, book_id=b.id) for b in books[:2]])
        await db.session.commit()
        yield SimpleNamespace(db=db, alert=alert, books=books)


async def total(journey):
    async with journey.db.maker() as session:
        return await session.scalar(
            select(Alert.series_total_cents).where(Alert.id == journey.alert.id)
        )


async def test_adding_already_priced_book_recalculates_without_collecting(journey, monkeypatch):
    notices = tuple(BookMetadata(title=b.title, isbn13=b.isbn13) for b in journey.books)
    monkeypatch.setattr(
        BookMetadataService,
        "lookup_series_notices",
        AsyncMock(return_value=CatalogueVerdict(consulted=("BNF",), notices=notices)),
    )
    price = AsyncMock()
    monkeypatch.setattr(catalog_linking, "schedule_book_pricing", price)
    assert await catalog_linking.link_books_from_bnf("Cible BD", journey.alert.id, series=True)
    assert await total(journey) == 6000
    price.assert_not_called()


async def test_link_change_is_invalidated_but_idempotent_link_is_not(journey):
    async with journey.db.maker() as session:
        repo = BookRepository(session)
        await repo.link_alert_to_book(journey.alert.id, journey.books[0].id)
        await session.commit()
    assert await total(journey) == 3000
    async with journey.db.maker() as session:
        await BookRepository(session).link_alert_to_book(journey.alert.id, journey.books[2].id)
        await session.commit()
    assert await total(journey) is None


async def test_remove_invalidates_preserves_shared_caches_and_is_atomic(journey):
    async with journey.db.maker() as session:
        await BookRepository(session).clear_alert_books(journey.alert.id)
        await session.rollback()
    assert await total(journey) == 3000
    async with journey.db.maker() as session:
        await BookRepository(session).clear_alert_books(journey.alert.id)
        await session.commit()
        assert len((await session.execute(select(Book))).scalars().all()) == 3
        assert len((await session.execute(select(BookMarketCache))).scalars().all()) == 3
    assert await total(journey) is None


@pytest.mark.parametrize("case", ["complete", "expired", "undated", "missing", "edition", "none"])
async def test_real_ingestion_and_market_panel_agree(journey, monkeypatch, case):
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    monkeypatch.setattr(tasks, "_background_tasks", set())
    monkeypatch.setattr(tasks, "_cleanup_tasks", set())
    monkeypatch.setattr(ingestion, "record_sent_cote_state", AsyncMock())
    monkeypatch.setattr(
            "app.services.book_market_pricing.schedule_book_pricing",
            lambda *_args, **_kwargs: None,
        )
    if case == "expired":
        journey.books[1].market_cache.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif case == "undated":
        journey.books[1].market_cache.expires_at = None
    elif case == "missing":
        await BookRepository(journey.db.session).clear_alert_books(journey.alert.id)
        await BookRepository(journey.db.session).link_alert_to_book(
            journey.alert.id, journey.books[0].id
        )
    elif case == "edition":
        journey.books[2].title = "Cible BD Tome 1"
        await BookRepository(journey.db.session).link_alert_to_book(
            journey.alert.id, journey.books[2].id
        )
    await journey.db.session.commit()
    title = "Lot Cible BD tomes 8 et 9" if case == "none" else "Lot Cible BD tomes 1 et 2"
    payload = ListingIngestPayload(
        source_name="synthetic",
        external_id="lot",
        title=title,
        category_slug="bd_comics",
        url="https://example.invalid/lot",
        item_price_cents=100,
    )
    bot = AsyncMock()
    bot.id = 123456789
    bot.send_message.return_value = telegram_message()
    async with journey.db.maker() as session:
        result = await ingestion.ProcessListingUseCase(session, bot).execute(payload, {})
        assert result.matches[0].notification_sent
    caption = bot.send_message.call_args.kwargs["text"]
    async with journey.db.maker() as session:
        panel = await ViewMarketDetailsUseCase(session).execute(result.listing_id, refresh=False)
        assert panel is not None
        text = MarketPriceService.format_market_details_card(
            panel.listing,
            panel.book,
            panel.books_in_series,
            panel.tome_result,
            series_total_cents=panel.series_total_cents,
        )
    if case == "complete":
        assert "Écart" in caption
        assert "30,00" in caption
        assert "Total du lot :</b> ~30,00" in text
    else:
        assert "Écart" not in caption
        assert "Total du lot" not in text
        assert "indisponible" in caption.lower()
        assert "indisponible" in text.lower()
    await tasks.wait_all_background_tasks(timeout=0)


async def test_replacement_recalculates_only_current_membership(journey):
    from app.services.book_market_pricing import recalculate_invalidated_series

    async with journey.db.maker() as session:
        repo = BookRepository(session)
        await repo.clear_alert_books(journey.alert.id)
        await repo.link_alert_to_book(journey.alert.id, journey.books[1].id)
        await repo.link_alert_to_book(journey.alert.id, journey.books[2].id)
        await recalculate_invalidated_series(session)
        await session.commit()
    assert await total(journey) == 5000


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="Concurrence PostgreSQL réelle requise"
)
@pytest.mark.parametrize("change", ["add", "remove", "replace", "price"])
async def test_recalculation_reads_links_after_waiting_for_mutation(journey, monkeypatch, change):
    from app.services import book_market_pricing as pricing

    waiting = asyncio.Event()
    committed = asyncio.Event()
    # Ces listes survivent à la coroutine et à la fermeture de sa session :
    # l'identity map faible ne peut pas évacuer les objets préchargés.
    held_books, held_caches, held_sessions = [], [], []
    readings = []
    original_lock = MarketPriceRepository.lock_series
    original_read = BookRepository.get_all_books_for_alert

    def assert_retained(session):
        for value in [*held_books, *held_caches]:
            state = inspect(value)
            assert state.persistent
            assert session.sync_session.identity_map[state.key] is value
        for book, cache in zip(held_books, held_caches, strict=True):
            assert inspect(book).attrs.market_cache.loaded_value is cache

    def assert_old_prices():
        assert [cache.lowest_delivered_price_cents for cache in held_caches] == [1000, 2000]

    async def read_current(repo, alert_id):
        books = await original_read(repo, alert_id)
        if held_sessions and repo.session is held_sessions[0]:
            for book in books:
                assert inspect(book).attrs.market_cache.loaded_value is not NO_VALUE
                cache = book.market_cache
                assert cache is not None
                if book.id in {old.id for old in held_books}:
                    index = next(i for i, old in enumerate(held_books) if old.id == book.id)
                    assert book is held_books[index]
                    assert cache is held_caches[index]
            readings.append({b.id: b.market_cache.lowest_delivered_price_cents for b in books})
        return books

    async def acquire_after_commit(repo, alert_id):
        if held_sessions and repo.session is held_sessions[0]:
            assert_retained(repo.session)
            assert_old_prices()
            waiting.set()
            await committed.wait()
            # Aucun refresh/expire de test entre l'écriture concurrente et le
            # vrai chemin central : l'ancienne cote est encore dans les objets.
            assert_retained(repo.session)
            assert_old_prices()
        await original_lock(repo, alert_id)

    monkeypatch.setattr(BookRepository, "get_all_books_for_alert", read_current)
    monkeypatch.setattr(MarketPriceRepository, "lock_series", acquire_after_commit)
    async with journey.db.maker() as editor:
        alert = await AlertRepository(editor).get_by_id(journey.alert.id)
        assert await AlertRepository(editor).lock_for_edit(alert)

        async def recalculate():
            async with journey.db.maker() as session:
                held_sessions.append(session)
                books = await BookRepository(session).get_all_books_for_alert(journey.alert.id)
                held_books.extend(books)
                held_caches.extend(book.market_cache for book in books)
                assert [book.id for book in held_books] == [b.id for b in journey.books[:2]]
                assert_retained(session)
                assert_old_prices()
                await pricing.refresh_series_total(session, journey.alert.id)
                assert_retained(session)
                if change == "price":
                    assert held_caches[0].lowest_delivered_price_cents == 1500
                await session.commit()

        task = asyncio.create_task(recalculate())
        await asyncio.wait_for(waiting.wait(), 2)
        assert_retained(held_sessions[0])
        assert_old_prices()
        if change == "add":
            await BookRepository(editor).link_alert_to_book(journey.alert.id, journey.books[2].id)
        elif change == "remove":
            await BookRepository(editor).clear_alert_books(journey.alert.id)
        elif change == "replace":
            repo = BookRepository(editor)
            await repo.clear_alert_books(journey.alert.id)
            await repo.link_alert_to_book(journey.alert.id, journey.books[1].id)
            await repo.link_alert_to_book(journey.alert.id, journey.books[2].id)
        else:
            from app.providers.base import MarketOffer

            offer = MarketOffer(
                provider_name="synthetic",
                item_price_cents=1500,
                observed_at=datetime.now(UTC),
                shipping_price_cents=0,
                delivered_price_cents=1500,
                source_url="https://example.invalid/book",
                item_condition="Très bon",
            )
            # Persistance centrale, sans consultation réseau, dans la transaction
            # qui possède déjà le verrou d'alerte.
            await pricing.persist_market_result(
                editor,
                journey.books[0].id,
                pricing.BookPricingResult.from_offers(
                    MarketOffers(
                        [offer],
                        attempts=(CollectionAttempt("synthetic", "current"),),
                    ),
                    offer,
                    None,
                ),
            )
        await editor.commit()
        assert_retained(held_sessions[0])
        assert_old_prices()
        committed.set()
        await asyncio.wait_for(task, 5)
    first, second, third = [b.id for b in journey.books]
    assert readings == [
        {first: 1000, second: 2000},
        {
            "add": {first: 1000, second: 2000, third: 3000},
            "remove": {},
            "replace": {second: 2000, third: 3000},
            "price": {first: 1500, second: 2000},
        }[change],
    ]
    assert len(held_books) == len(held_caches) == 2
    expected = {"add": 6000, "remove": None, "replace": 5000, "price": 3500}
    assert await total(journey) == expected[change]


async def test_concordant_isbn_links_the_cached_edition_without_an_author(journey, monkeypatch):
    target = journey.books[2]
    target.author = None
    target.language = "fr"
    journey.alert.name = target.title
    journey.alert.isbn = target.isbn13
    await BookRepository(journey.db.session).clear_alert_books(journey.alert.id)
    await journey.db.session.commit()

    def reject_discovery(coro, **_kwargs):
        coro.close()
        pytest.fail("L’ISBN concordant en cache ne doit pas lancer de découverte")

    monkeypatch.setattr(catalog_linking, "safe_create_task", reject_discovery)
    async with journey.db.maker() as session:
        for _ in range(2):
            book = await catalog_linking.resolve_bd_catalog_book(
                session, alert_id=journey.alert.id, alert_name=target.title,
                alert_isbn=target.isbn13, listing_title=target.title,
                listing_description=None, author=None,
            )
            assert book is not None
            assert book.id == target.id
            assert book.isbn13 == target.isbn13
            assert book.author is None
            await session.commit()
    async with journey.db.maker() as reader:
        links = list((await reader.scalars(
            select(AlertBook).where(AlertBook.alert_id == journey.alert.id)
        )).all())
        assert [link.book_id for link in links] == [target.id]
        assert len(list((await reader.scalars(select(Book))).all())) == 3
