from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.book_repo import BookRepository
from app.services.market_refresh_policy import record_component
from tests.database import postgres_replayed_database


async def _books(session, slots):
    user = User(telegram_user_id=424242, is_allowed=True)
    category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
    session.add_all([user, category])
    await session.flush()
    books = []
    for index, slot in enumerate(slots):
        book = Book(title=f"Livre {index}", normalized_title=f"livre {index}")
        alert = Alert(user_id=user.id, category_id=category.id, name=book.title,
                      include_keywords=["livre"])
        session.add_all([book, alert])
        await session.flush()
        session.add(AlertBook(alert_id=alert.id, book_id=book.id))
        session.add(BookMarketCache(book_id=book.id, next_refresh_at=slot))
        books.append(book)
    await session.commit()
    return books


@pytest.mark.parametrize(("deadline", "expected"), [
    ("2026-10-22T12:07:00", "2026-10-22T12:15:00"),
    ("2026-10-22T12:15:00", "2026-10-22T12:15:00"),
    ("2026-10-22T12:15:01", "2026-10-22T12:45:00"),
    ("2026-10-22T12:15:00.000001", "2026-10-22T12:45:00"),
    ("2026-10-22T12:46:00", "2026-10-22T13:15:00"),
    ("2026-10-22T23:46:00", "2026-10-23T00:15:00"),
    ("2026-10-22T11:59:00", "2026-10-22T11:59:00"),
    ("2026-10-22T12:00:00", "2026-10-22T12:00:00"),
])
async def test_refresh_deadlines_are_persisted_on_the_grid_without_moving_due_books(
    deadline, expected,
):
    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    async with postgres_replayed_database() as db:
        book, = await _books(db.session, [datetime.fromisoformat(deadline).replace(tzinfo=UTC)])
        for _ in range(2):
            await BookRepository(db.session).plan_price_refreshes(now=now)
            await db.session.commit()
        async with db.maker() as reader:
            stored = await BookRepository(reader).get_by_id(book.id)
            assert stored.market_cache.next_refresh_at.replace(tzinfo=UTC) == (
                datetime.fromisoformat(expected).replace(tzinfo=UTC)
            )


async def test_due_population_progresses_without_moving_overflow_into_the_future():
    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    overdue = now - timedelta(hours=2)
    async with postgres_replayed_database() as db:
        books = await _books(db.session, [overdue] * 19)
        remaining = {book.id for book in books}
        for index in range(3):
            moment = now + timedelta(minutes=30 * index)
            await BookRepository(db.session).plan_price_refreshes(now=moment)
            await db.session.commit()
            async with db.maker() as session:
                repo = BookRepository(session)
                all_books = await repo.get_books_for_active_bd_alerts()
                assert all(book.market_cache.next_refresh_at.replace(tzinfo=UTC) == overdue
                           for book in all_books if book.id in remaining)
                batch = await repo.get_books_needing_price_refresh(limit=8, now=moment)
                assert len(batch) == min(8, len(remaining))
                assert {book.id for book in batch} <= remaining
                for book in batch:
                    for component in ("current", "history"):
                        record_component(book.market_cache, component, succeeded=True, now=moment)
                    remaining.remove(book.id)
                await session.commit()
            db.session.expire_all()
        assert remaining == set()
