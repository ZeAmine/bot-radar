"""Un échec de collecte ne remet pas le même livre en tête de chaque passe."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.constants import ComparisonStatus
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.book_repo import FAILED_COLLECTION_REST_HOURS, BookRepository
from app.providers.base import MarketOffers
from app.services.book_market_pricing import price_book
from tests.database import sqlite_only_database


async def _catalogue(session) -> tuple[User, Category]:
    category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
    user = User(telegram_user_id=1, is_allowed=True)
    session.add_all([category, user])
    await session.flush()
    return user, category


async def _linked_book(session, user: User, category: Category, *, title: str) -> Book:
    alert = Alert(
        user_id=user.id,
        category_id=category.id,
        name=title,
        include_keywords=["livre"],
    )
    book = Book(title=title, normalized_title=title.lower())
    session.add_all([alert, book])
    await session.flush()
    session.add(AlertBook(alert_id=alert.id, book_id=book.id))
    return book


@pytest.mark.asyncio
async def test_a_recent_failed_collection_waits_before_the_next_incremental_pass():
    """Le lot de 8 rejouait les mêmes échecs : checked_at restait vide, donc en tête."""
    now = datetime.now(UTC)
    async with sqlite_only_database() as database:
        session = database.session
        user, category = await _catalogue(session)
        resting = await _linked_book(session, user, category, title="Au repos")
        due = await _linked_book(session, user, category, title="Du")
        fresh = await _linked_book(session, user, category, title="Jamais")
        session.add_all(
            [
                BookMarketCache(
                    book_id=resting.id,
                    comparison_status=ComparisonStatus.UNAVAILABLE,
                    collection_status="failed",
                    checked_at=None,
                    last_attempt_at=now - timedelta(hours=1),
                    failed_provider_names=["chasse_aux_livres"],
                ),
                BookMarketCache(
                    book_id=due.id,
                    comparison_status=ComparisonStatus.UNAVAILABLE,
                    collection_status="failed",
                    checked_at=None,
                    last_attempt_at=now - timedelta(hours=FAILED_COLLECTION_REST_HOURS, minutes=5),
                    failed_provider_names=["chasse_aux_livres"],
                ),
            ]
        )
        await session.commit()

        repo = BookRepository(session)
        await repo.plan_price_refreshes(now=now)
        selected = await repo.get_books_needing_price_refresh(
            limit=8, now=now+timedelta(minutes=31),
        )

    ids = {book.id for book in selected}
    assert resting.id not in ids
    assert due.id in ids
    assert fresh.id in ids


@pytest.mark.asyncio
async def test_failed_collection_keeps_the_attempt_date():
    """La tentative échouée reste datée sans devenir un relevé réussi."""
    async with sqlite_only_database() as database:
        session = database.session
        book = Book(title="Livre", normalized_title="livre")
        session.add(book)
        await session.commit()
        registry = MagicMock()
        registry.fetch_all_offers = AsyncMock(
            return_value=MarketOffers(failed_providers=("chasse_aux_livres",))
        )
        cache = await price_book(session, book, registry=registry)
        await session.commit()

    assert cache.collection_status == "failed"
    assert cache.checked_at is None
    assert cache.last_attempt_at is not None
    assert cache.last_successful_collection_at is None


async def test_paused_books_are_refreshed_but_deleted_books_are_not():
    from app.domain.enums import AlertStatus

    async with sqlite_only_database() as database:
        session = database.session
        user, category = await _catalogue(session)
        paused = await _linked_book(session, user, category, title="Pause")
        deleted = await _linked_book(session, user, category, title="Suppression")
        from sqlalchemy import select
        for alert in (await session.scalars(select(Alert))).all():
            alert.status = (AlertStatus.PAUSED if alert.name == "Pause"
                            else AlertStatus.DELETED)
        await session.commit()
        repo = BookRepository(session)
        assert {b.id for b in await repo.get_books_for_active_bd_alerts()} == {paused.id}
        now = datetime.now(UTC)
        await repo.plan_price_refreshes(now=now)
        assert {b.id for b in await repo.get_books_needing_price_refresh(
            now=now+timedelta(minutes=31),
        )} == {paused.id}
        assert deleted.id != paused.id


async def test_failed_retry_does_not_starve_an_older_unattempted_book():
    now = datetime.now(UTC)
    async with sqlite_only_database() as database:
        session = database.session
        user, category = await _catalogue(session)
        failure = await _linked_book(session, user, category, title="Échec")
        waiting = await _linked_book(session, user, category, title="Attente")
        session.add_all([
            BookMarketCache(book_id=failure.id, collection_status="failed",
                            last_attempt_at=now-timedelta(hours=7)),
            BookMarketCache(book_id=waiting.id, checked_at=now-timedelta(days=3),
                            expires_at=now-timedelta(days=2)),
        ])
        await session.commit()
        repo = BookRepository(session)
        await repo.plan_price_refreshes(now=now, capacity=1)
        selected = await repo.get_books_needing_price_refresh(
            limit=1, now=now+timedelta(minutes=31),
        )
        assert [book.id for book in selected] == [waiting.id]


async def test_successful_empty_collection_is_fresh_for_selection():
    now = datetime.now(UTC)
    async with sqlite_only_database() as database:
        session = database.session
        user, category = await _catalogue(session)
        book = await _linked_book(session, user, category, title="Vide récemment")
        session.add(BookMarketCache(
            book_id=book.id, collection_status="empty", checked_at=now-timedelta(days=4),
            last_attempt_at=now, last_successful_collection_at=now,
        ))
        await session.commit()
        assert await BookRepository(session).get_books_needing_price_refresh() == []


async def test_larger_population_has_capacity_and_no_retry_starvation():
    async with sqlite_only_database() as database:
        session = database.session
        user, category = await _catalogue(session)
        ids = set()
        for n in range(120):
            ids.add((await _linked_book(session, user, category, title=f"Livre {n}")).id)
        await session.commit()
        seen = set()
        repo = BookRepository(session)
        now = datetime.now(UTC)
        await repo.plan_price_refreshes(now=now)
        for batch in range(15):
            books = await repo.get_books_needing_price_refresh(
                limit=8, now=now+timedelta(minutes=31+30*batch),
            )
            assert len(books) == 8
            for book in books:
                assert book.id not in seen
                seen.add(book.id)
                book.market_cache.collection_status = "failed"
                book.market_cache.last_attempt_at = now
                book.market_cache.next_refresh_at = now+timedelta(days=7)
            await session.commit()
        assert seen == ids


async def test_attempt_date_without_success_is_not_current_collection_freshness():
    from app.db.repositories.market_price_repo import MarketPriceRepository

    now = datetime.now(UTC)
    async with sqlite_only_database() as db:
        user, category = await _catalogue(db.session)
        book = await _linked_book(db.session, user, category, title="Historique seul")
        book.created_at = now-timedelta(days=3)
        db.session.add(BookMarketCache(
            book_id=book.id, collection_status="partial", checked_at=now,
            last_attempt_at=now, historical_low_1y_cents=545,
        ))
        await db.session.commit()
        rows, total = await MarketPriceRepository(db.session).find_stale_current_collections(
            stale_after=now-timedelta(hours=48),
        )
        assert total == 1
        assert rows[0][0] == "Historique seul"


async def test_internal_collection_error_keeps_attempt_and_allows_other_books(monkeypatch):
    from app.services import book_market_pricing as pricing

    async with sqlite_only_database() as database:
        session = database.session
        user, category = await _catalogue(session)
        await _linked_book(session, user, category, title="Premier")
        await _linked_book(session, user, category, title="Second")
        await session.commit()
        repo = BookRepository(session)
        now = datetime.now(UTC)
        await repo.plan_price_refreshes(now=now)
        await session.commit()
        first = (await repo.get_books_needing_price_refresh(
            limit=1, now=now+timedelta(minutes=31),
        ))[0]
        monkeypatch.setattr(pricing, "AsyncSessionLocal", database.maker)
        registry = MagicMock()
        registry.fetch_all_offers = AsyncMock(side_effect=RuntimeError("collecte interrompue"))
        assert await pricing._price_book_once(first.id, reason="test", registry=registry) is None
        async with database.maker() as verification:
            cache = await pricing.MarketPriceRepository(verification).get_cache_by_book_id(first.id)
            assert cache is not None
            assert cache.last_attempt_at is not None
            assert cache.collection_status == "failed"
            assert cache.last_successful_collection_at is None
            assert cache.checked_at is None
            following = await BookRepository(verification).get_books_needing_price_refresh(
                limit=1, now=now+timedelta(minutes=31),
            )
            assert len(following) == 1
            assert following[0].id != first.id


async def test_overdue_queue_keeps_one_batch_and_reserves_future_slots():
    now = datetime.now(UTC)
    async with sqlite_only_database() as database:
        session = database.session
        user, category = await _catalogue(session)
        books = [await _linked_book(session, user, category, title=f"Attente {i}")
                 for i in range(19)]
        for book in books:
            session.add(BookMarketCache(book_id=book.id,
                        next_refresh_at=now-timedelta(hours=2)))
        await session.commit()
        repo = BookRepository(session)
        await repo.plan_price_refreshes(now=now)
        selected = await repo.get_books_needing_price_refresh(now=now)
        assert len(selected) == 8
        slots = [book.market_cache.next_refresh_at for book in books
                 if book not in selected]
        assert all(slot.replace(tzinfo=UTC) > now for slot in slots)
        assert max(slots.count(slot) for slot in slots) <= 8
