from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.db.models import Alert, AlertBook, Book, BookMarketCache, Category, User
from app.db.repositories.book_repo import BookRepository
from app.providers.base import CollectionAttempt, MarketOffer, MarketOffers
from app.services.book_market_pricing import (
    BookPricingResult,
    persist_market_result,
    price_book_now,
)
from tests.database import postgres_replayed_database

pytestmark = pytest.mark.asyncio


async def seed(session):
    user = User(telegram_user_id=424242, is_allowed=True)
    category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
    session.add_all([user, category])
    await session.flush()
    alert = Alert(
        user_id=user.id, category_id=category.id, name="Livre", include_keywords=["livre"]
    )
    book = Book(title="Livre", normalized_title="livre", isbn13="9782205049657")
    session.add_all([alert, book])
    await session.flush()
    session.add(AlertBook(alert_id=alert.id, book_id=book.id))
    await session.commit()
    return book


@pytest.mark.parametrize("outcome", ["history", "failed", "current"])
async def test_direct_persistence_reserves_the_next_eligible_pass(outcome):
    async with postgres_replayed_database() as db:
        book = await seed(db.session)
        now = datetime.now(UTC)
        point = MarketOffer(
            "chasse_aux_livres",
            900,
            None,
            900,
            "https://example.com/history",
            item_condition="Très bon état +",
            is_historical=True,
            is_available=False,
            price_includes_shipping=True,
            observed_at=now - timedelta(days=1),
        )
        if outcome == "history":
            offers = MarketOffers(
                [point],
                historical_checked=True,
                attempts=(
                    CollectionAttempt("chasse_aux_livres", "current", "skipped", "not_requested"),
                    CollectionAttempt("chasse_aux_livres", "history", "success"),
                ),
            )
        elif outcome == "failed":
            offers = MarketOffers(
                [],
                failed_providers=("chasse_aux_livres",),
                attempts=(CollectionAttempt("chasse_aux_livres", "current", "failed", "timeout"),),
            )
        else:
            point = MarketOffer(
                "source",
                900,
                100,
                1000,
                "https://example.com/current",
                item_condition="Très bon",
                price_includes_shipping=True,
            )
            offers = MarketOffers([point], attempts=(CollectionAttempt("source", "current"),))
        result = BookPricingResult.from_offers(
            offers,
            point if outcome == "current" else None,
            900 if outcome == "history" else None,
        )
        written = await persist_market_result(db.session, book.id, result)
        await db.session.commit()
        async with db.maker() as reader:
            cache = await reader.get(BookMarketCache, written.id)
            assert cache.next_refresh_at is not None
            assert cache.next_refresh_at.replace(tzinfo=UTC) > now
            # Plus de grille de créneaux : l'échéance vient de l'état des
            # composantes, et c'est à leur rythme qu'elle est datée.
            assert cache.refresh_state, "l'échéance doit venir de l'état des composantes"
            if outcome == "history":
                assert cache.last_successful_collection_at is None
                assert cache.lowest_delivered_price_cents is None
                assert cache.source_history["price_cents"] == 900


@pytest.mark.parametrize("slot", ["missing", "past", "before_due"])
async def test_a_fresh_cache_is_neither_collected_nor_requeued(monkeypatch, slot):
    """Un cache frais n'est ni collecté, ni remis dans la file, ni crédité d'un relevé.

    L'ancienne version exigeait que la réservation « répare » l'échéance d'un livre
    frais (>= due). Le modèle de fraîcheur rend cette réparation inutile : un livre
    frais n'est pas dû, donc il n'est pas sélectionné, qu'il porte une échéance
    passée ou aucune. Ce qui reste à prouver : aucune collecte, aucun succès
    inventé, et une échéance posée quand il n'y en a aucune.
    """
    async with postgres_replayed_database() as db:
        book = await seed(db.session)
        now = datetime.now(UTC)
        success = now - timedelta(days=1)
        due = success + timedelta(days=7)
        cache = BookMarketCache(
            book_id=book.id,
            last_successful_collection_at=success,
            historical_checked_at=success,
            collection_status="complete",
            next_refresh_at=None
            if slot == "missing"
            else now - timedelta(hours=1)
            if slot == "past"
            else now + timedelta(hours=1),
        )
        db.session.add(cache)
        await db.session.commit()
        monkeypatch.setattr("app.services.book_market_pricing.AsyncSessionLocal", db.maker)
        fetch = AsyncMock(side_effect=AssertionError("No collection expected"))
        monkeypatch.setattr("app.services.book_market_pricing.fetch_offers_for_book", fetch)
        await price_book_now(book.id, reason="test")
        fetch.assert_not_awaited()
        async with db.maker() as reader:
            persisted = await reader.get(BookMarketCache, cache.id)
            assert persisted.next_refresh_at is not None
            if slot == "missing":
                assert persisted.next_refresh_at.replace(tzinfo=UTC) == due
            assert persisted.last_successful_collection_at.replace(tzinfo=UTC) == success
            selected = await BookRepository(reader).get_books_needing_price_refresh(
                limit=8, now=now,
            )
            assert book.id not in {chosen.id for chosen in selected}


async def test_recovery_reserves_without_inventing_a_success(monkeypatch):
    async with postgres_replayed_database() as db:
        book = await seed(db.session)
        monkeypatch.setattr("app.services.book_market_pricing.AsyncSessionLocal", db.maker)
        monkeypatch.setattr(
            "app.services.book_market_pricing.price_book",
            AsyncMock(side_effect=RuntimeError("synthetic")),
        )
        assert await price_book_now(book.id, reason="test") is None
        from sqlalchemy import select

        async with db.maker() as reader:
            cache = await reader.scalar(
                select(BookMarketCache).where(BookMarketCache.book_id == book.id)
            )
            assert cache.collection_status == "failed"
            assert cache.next_refresh_at is not None
            assert cache.last_successful_collection_at is None
            assert cache.historical_checked_at is None


async def test_administrative_retry_is_not_replanned(monkeypatch):
    async with postgres_replayed_database() as db:
        book = await seed(db.session)
        attempts = (
            CollectionAttempt(
                "chasse_aux_livres", "current", "skipped", "pass_budget", retry_after_seconds=7200
            ),
        )
        result = BookPricingResult.from_offers(MarketOffers([], attempts=attempts), None, None)
        planner = AsyncMock()
        monkeypatch.setattr(
            "app.db.repositories.book_repo.BookRepository.plan_price_refreshes", planner
        )
        written = await persist_market_result(db.session, book.id, result)
        await db.session.commit()
        planner.assert_not_awaited()
        async with db.maker() as reader:
            cache = await reader.get(BookMarketCache, written.id)
            assert cache.next_refresh_at.replace(tzinfo=UTC) == result.collected_at + timedelta(
                hours=2
            )
            assert cache.collection_status == "deferred"
            assert cache.last_successful_collection_at is None
            assert cache.historical_checked_at is None
