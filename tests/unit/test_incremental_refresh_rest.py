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

        selected = await BookRepository(session).get_books_needing_price_refresh(limit=8)

    ids = {book.id for book in selected}
    assert resting.id not in ids
    assert due.id in ids
    assert fresh.id in ids


@pytest.mark.asyncio
async def test_failed_collection_keeps_the_attempt_date():
    """Effacer checked_at faisait croire que le livre n'avait jamais été tenté."""
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
    assert cache.checked_at is not None
    assert cache.last_attempt_at is not None
