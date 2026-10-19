"""Maintenance nocturne des livres : plage, lot borné et reprise après redémarrage."""

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import Settings
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.book_repo import BookRepository
from app.providers.base import MarketOffers
from app.services.book_market_pricing import price_book
from app.services.market_refresh_policy import record_component
from app.tasks import night_book_maintenance
from app.tasks.night_book_maintenance import (
    NIGHT_MAINTENANCE_END_MARGIN_SECONDS,
    NIGHT_MAINTENANCE_PAUSE_RANGE,
    empty_stats,
    night_timezone,
    run_night_book_maintenance,
)
from tests.database import sqlite_only_database


@pytest.fixture(autouse=True)
def _reset_night_counters():
    night_book_maintenance._lots_done = 0
    night_book_maintenance._editions_done = 0
    return


STATS = {
    "processed": 3,
    "refreshed": 2,
    "empty": 0,
    "reused": 0,
    "available": 2,
    "unavailable": 1,
    "degraded": 0,
    "errors": 0,
}


async def _catalogue(session) -> tuple[User, Category]:
    category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
    user = User(telegram_user_id=1, is_allowed=True)
    session.add_all([category, user])
    await session.flush()
    return user, category


async def _linked_book(session, user: User, category: Category, *, title: str) -> Book:
    alert = Alert(user_id=user.id, category_id=category.id, name=title,
                  include_keywords=["livre"])
    book = Book(title=title, normalized_title=title.lower())
    session.add_all([alert, book])
    await session.flush()
    session.add(AlertBook(alert_id=alert.id, book_id=book.id))
    return book


@pytest.mark.asyncio
async def test_a_disabled_night_mode_never_starts_a_maintenance_lot():
    with (
        patch.object(night_book_maintenance.settings, "NIGHT_MODE_ENABLED", False),
        patch.object(night_book_maintenance, "is_night_time", return_value=True),
        patch.object(night_book_maintenance, "refresh_book_prices_task",
                     new_callable=AsyncMock) as pass_task,
    ):
        stats = await run_night_book_maintenance()

    assert stats == empty_stats()
    pass_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_outside_the_window_no_lot_starts_even_with_time_left():
    with (
        patch.object(night_book_maintenance.settings, "NIGHT_MODE_ENABLED", True),
        patch.object(night_book_maintenance, "is_night_time", return_value=False),
        patch.object(night_book_maintenance, "get_night_mode_remaining_seconds",
                     return_value=3600.0),
        patch.object(night_book_maintenance, "refresh_book_prices_task",
                     new_callable=AsyncMock) as pass_task,
    ):
        stats = await run_night_book_maintenance()

    assert stats == empty_stats()
    pass_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_lot_uses_the_configured_batch_the_long_pauses_and_a_bounded_budget():
    with (
        patch.object(night_book_maintenance.settings, "NIGHT_MODE_ENABLED", True),
        patch.object(night_book_maintenance.settings, "NIGHT_MAINTENANCE_BATCH_SIZE", 4),
        patch.object(night_book_maintenance, "is_night_time", return_value=True),
        patch.object(night_book_maintenance, "get_night_mode_remaining_seconds",
                     return_value=3600.0),
        patch.object(night_book_maintenance, "refresh_book_prices_task",
                     new_callable=AsyncMock, return_value=STATS) as pass_task,
    ):
        stats = await run_night_book_maintenance()

    assert stats == STATS
    pass_task.assert_awaited_once_with(
        incremental=True,
        batch_size=4,
        max_duration_seconds=3600.0 - NIGHT_MAINTENANCE_END_MARGIN_SECONDS,
        job_id="night_book_maintenance",
        pause_range=NIGHT_MAINTENANCE_PAUSE_RANGE,
    )


@pytest.mark.asyncio
async def test_a_custom_overnight_window_bounds_the_budget():
    with (
        patch.object(night_book_maintenance.settings, "NIGHT_MODE_ENABLED", True),
        patch.object(night_book_maintenance.settings, "NIGHT_MAINTENANCE_BATCH_SIZE", 10),
        patch.object(night_book_maintenance, "is_night_time", return_value=True),
        patch.object(night_book_maintenance, "get_night_mode_remaining_seconds",
                     return_value=2 * 3600.0),
        patch.object(night_book_maintenance, "refresh_book_prices_task",
                     new_callable=AsyncMock, return_value=STATS) as pass_task,
    ):
        await run_night_book_maintenance()

    assert pass_task.await_args.kwargs["max_duration_seconds"] == (
        2 * 3600.0 - NIGHT_MAINTENANCE_END_MARGIN_SECONDS
    )


@pytest.mark.asyncio
async def test_a_window_that_ends_right_away_starts_no_lot():
    with (
        patch.object(night_book_maintenance.settings, "NIGHT_MODE_ENABLED", True),
        patch.object(night_book_maintenance, "is_night_time", return_value=True),
        patch.object(night_book_maintenance, "get_night_mode_remaining_seconds",
                     return_value=NIGHT_MAINTENANCE_END_MARGIN_SECONDS - 1),
        patch.object(night_book_maintenance, "refresh_book_prices_task",
                     new_callable=AsyncMock) as pass_task,
    ):
        stats = await run_night_book_maintenance()

    assert stats == empty_stats()
    pass_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_window_close_stops_new_lots_and_report_reports_the_last_night(caplog):
    caplog.set_level(logging.INFO, logger="app.tasks.night_book_maintenance")
    with (
        patch.object(night_book_maintenance.settings, "NIGHT_MODE_ENABLED", True),
        patch.object(night_book_maintenance.settings, "NIGHT_MAINTENANCE_BATCH_SIZE", 10),
        patch.object(night_book_maintenance, "is_night_time",
                     side_effect=[True, True, False]),
        patch.object(night_book_maintenance, "get_night_mode_remaining_seconds",
                     return_value=3600.0),
        patch.object(night_book_maintenance, "refresh_book_prices_task",
                     new_callable=AsyncMock, return_value=STATS) as pass_task,
    ):
        await run_night_book_maintenance()
        await run_night_book_maintenance()
        stats = await run_night_book_maintenance()

    assert stats == empty_stats()
    assert pass_task.await_count == 2
    assert "Maintenance nocturne close : 2 lot(s), 6 édition(s)" in caplog.text


def test_batch_size_is_configurable_and_defaults_to_ten(monkeypatch):
    monkeypatch.delenv("NIGHT_MAINTENANCE_BATCH_SIZE", raising=False)
    assert Settings(_env_file=None).NIGHT_MAINTENANCE_BATCH_SIZE == 10
    monkeypatch.setenv("NIGHT_MAINTENANCE_BATCH_SIZE", "4")
    assert Settings(_env_file=None).NIGHT_MAINTENANCE_BATCH_SIZE == 4


def test_night_timezone_falls_back_to_paris_on_an_invalid_value():
    assert night_timezone() == "Europe/Paris"
    with patch.object(night_book_maintenance.settings, "NIGHT_MODE_TIMEZONE", "Mars/Olympus"):
        assert night_timezone() == "Europe/Paris"
    with patch.object(night_book_maintenance.settings, "NIGHT_MODE_TIMEZONE", "America/New_York"):
        assert night_timezone() == "America/New_York"


@pytest.mark.asyncio
async def test_a_restart_in_the_middle_of_the_window_keeps_the_reservations():
    now = datetime.now(UTC).replace(microsecond=0)
    async with sqlite_only_database() as database:
        session = database.session
        user, category = await _catalogue(session)
        first = await _linked_book(session, user, category, title="Premier")
        second = await _linked_book(session, user, category, title="Second")
        await session.commit()
        await BookRepository(session).plan_price_refreshes(now=now, capacity=8)
        await session.commit()

        async with database.maker() as restarted:
            selected = await BookRepository(restarted).get_books_needing_price_refresh(
                limit=8, now=now + timedelta(minutes=31),
            )

    assert {book.id for book in selected} == {first.id, second.id}


@pytest.mark.asyncio
async def test_night_lots_rotate_through_the_whole_population_without_repeating():
    now = datetime(2026, 10, 19, 1, 0, tzinfo=UTC)
    async with sqlite_only_database() as database:
        session = database.session
        user, category = await _catalogue(session)
        books = [
            await _linked_book(session, user, category, title=f"Édition {index}")
            for index in range(12)
        ]
        await session.commit()
        repo = BookRepository(session)
        seen: set = set()
        for lot in range(3):
            planned_at = now + timedelta(minutes=31 * lot)
            await repo.plan_price_refreshes(now=planned_at, capacity=5)
            selected = await repo.get_books_needing_price_refresh(
                limit=5, now=planned_at + timedelta(minutes=31),
            )
            assert 0 < len(selected) <= 5
            for book in selected:
                assert book.id not in seen
                seen.add(book.id)
                cache = book.market_cache
                assert cache is not None
                record_component(cache, "current", succeeded=True, now=planned_at)
                record_component(cache, "history", succeeded=True, now=planned_at)
                cache.last_successful_collection_at = planned_at
                cache.historical_checked_at = planned_at
            await session.commit()

    assert seen == {book.id for book in books}


@pytest.mark.asyncio
async def test_a_known_book_is_not_recollected_after_a_restart_while_still_fresh():
    now = datetime.now(UTC).replace(microsecond=0)
    async with sqlite_only_database() as database:
        session = database.session
        user, category = await _catalogue(session)
        book = await _linked_book(session, user, category, title="Déjà collecté")
        cache = BookMarketCache(book_id=book.id, collection_status="complete")
        session.add(cache)
        record_component(cache, "current", succeeded=True, now=now - timedelta(hours=2))
        record_component(cache, "history", succeeded=True, now=now - timedelta(hours=2))
        cache.last_successful_collection_at = now - timedelta(hours=2)
        cache.historical_checked_at = now - timedelta(hours=2)
        cache.historical_low_1y_cents = 900
        cache.source_history = {"price_cents": 900, "condition": "Très bon état +"}
        await session.commit()

        await BookRepository(session).plan_price_refreshes(now=now, capacity=10)
        assert await BookRepository(session).get_books_needing_price_refresh(
            limit=10, now=now,
        ) == []
        await session.commit()

        async with database.maker() as restarted:
            await BookRepository(restarted).plan_price_refreshes(now=now, capacity=10)
            assert await BookRepository(restarted).get_books_needing_price_refresh(
                limit=10, now=now,
            ) == []


@pytest.mark.asyncio
async def test_a_failed_lot_collection_keeps_the_last_known_data():
    now = datetime.now(UTC).replace(microsecond=0)
    async with sqlite_only_database() as database:
        session = database.session
        user, category = await _catalogue(session)
        book = await _linked_book(session, user, category, title="Dernière offre connue")
        cache = BookMarketCache(
            book_id=book.id,
            collection_status="complete",
            lowest_delivered_price_cents=1200,
            comparison_status="available",
            historical_low_1y_cents=900,
            last_successful_collection_at=now - timedelta(days=6),
            historical_checked_at=now - timedelta(days=6),
            current_offers=[{
                "provider_name": "chasse_aux_livres",
                "item_price_cents": 1000,
                "shipping_price_cents": 200,
                "delivered_price_cents": 1200,
                "source_url": "https://exemple.test/offre",
            }],
        )
        session.add(cache)
        await session.commit()

        class FailedRegistry:
            failed_provider_names: set[str] = set()

            @staticmethod
            async def fetch_all_offers(**_kwargs):
                return MarketOffers(failed_providers=("chasse_aux_livres",))

        refreshed = await price_book(session, book, registry=FailedRegistry())
        await session.commit()

    assert refreshed.lowest_delivered_price_cents == 1200
    assert refreshed.historical_low_1y_cents == 900
    assert refreshed.last_successful_collection_at.replace(tzinfo=UTC) == now - timedelta(days=6)
    assert refreshed.current_offers
