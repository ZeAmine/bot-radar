"""Une collecte tardive ne réouvre pas un panneau fermé ou un ancien tome."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message

from app.bot.handlers import market_panel
from app.core.constants import AlertStatus
from app.db.models.book import Book
from app.use_cases.view_market_details import MarketPanelData


def test_series_buttons_only_offer_numbered_volumes():
    from app.bot.handlers.callbacks import _tome_selection_rows

    books = [
        Book(id=uuid.uuid4(), title=title, normalized_title=title.lower())
        for title in ("Série", "Série, Volume 2", "Série, Vol. 1")
    ]
    rows = _tome_selection_rows(uuid.uuid4(), books)
    assert [button.text for row in rows for button in row] == ["T1", "T2"]


def test_panel_has_no_global_series_button():
    books = [
        Book(id=uuid.uuid4(), title=f"Série Tome {n}", normalized_title=f"serie {n}")
        for n in (1, 2)
    ]
    view = market_panel.PanelView(
        MagicMock(spec=Message),
        MarketPanelData(None, books[0], books, None, None),
        listing_id=uuid.uuid4(),
        selected_id=books[0].id,
    )
    _, keyboard = market_panel._content(view)
    assert [button.text for row in keyboard.inline_keyboard for button in row] == [
        "T1",
        "T2",
        "Fermer",
    ]


async def test_closing_a_panel_prevents_late_updates():
    message = MagicMock(spec=Message)
    message.chat = MagicMock(id=1)
    message.message_id = 2
    message.edit_text = AsyncMock()
    book = Book(id=uuid.uuid4(), title="Série Tome 1", normalized_title="serie 1")
    panel = MarketPanelData(None, book, [book], None, None)
    view = market_panel.PanelView(message, panel, listing_id=uuid.uuid4())
    market_panel._panels[(1, 2)] = view
    market_panel.close_panel(message)
    await market_panel.render_panel(view)
    message.edit_text.assert_not_awaited()


async def test_render_uses_current_selection_instead_of_the_first_tome():
    message = MagicMock(spec=Message)
    message.chat = MagicMock(id=1)
    message.message_id = 2
    message.edit_text = AsyncMock()
    books = [
        Book(id=uuid.uuid4(), title=f"Série Tome {n}", normalized_title=f"serie {n}")
        for n in (1, 2)
    ]
    panel = MarketPanelData(None, books[0], books, None, None)
    view = market_panel.PanelView(message, panel, listing_id=uuid.uuid4())
    view.selected_id = books[1].id
    await market_panel.render_panel(view)
    assert "Série Tome 2" in message.edit_text.await_args.args[0]


def test_missing_first_tome_keeps_its_note_and_the_freshness_footer():
    from datetime import UTC, datetime

    from app.db.models.book_market_cache import BookMarketCache

    books = [
        Book(id=uuid.uuid4(), title=f"Série Tome {n}", normalized_title=f"serie {n}")
        for n in (2, 3)
    ]
    books[0].market_cache = BookMarketCache(
        historical_low_1y_cents=1000,
        collection_status="partial",
        last_attempt_at=datetime(2026, 10, 7, tzinfo=UTC),
    )
    view = market_panel.PanelView(
        MagicMock(spec=Message),
        MarketPanelData(None, books[0], books, None, None),
        alert_id=uuid.uuid4(),
        selected_id=books[0].id,
    )
    text, _ = market_panel._content(view)
    assert "Actualisation" not in text
    assert "Tome 1 absent du catalogue" in text
    assert "Dernière vérif. : inconnue" in text


def _panel_callback():
    reply_message = MagicMock(spec=Message)
    reply_message.chat = MagicMock(id=1)
    reply_message.message_id = 2
    callback = MagicMock()
    callback.message = MagicMock(spec=Message)
    callback.message.chat = MagicMock(id=1)
    callback.message.message_id = 1
    callback.message.reply = AsyncMock(return_value=reply_message)
    return callback


def _book_with_cache(cache):
    from app.db.models.book import Book

    book = Book(id=uuid.uuid4(), title="Série Tome 1", normalized_title="serie 1")
    book.market_cache = cache
    return book


async def _open(book, *, books=None, alert=None, tome_result=None):
    from app.bot.handlers import market_panel
    from app.use_cases.view_market_details import MarketPanelData

    books = books if books is not None else ([book] if book else [])
    panel = MarketPanelData(None, book, books, tome_result, None, alert=alert)
    callback = _panel_callback()
    await market_panel.open_panel(callback, panel)
    return callback.message.reply.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_open_panel_with_a_usable_price_does_not_start_a_collection():
    from datetime import UTC, datetime, timedelta
    from unittest.mock import patch

    from app.core.constants import ComparisonStatus
    from app.db.models.book_market_cache import BookMarketCache

    now = datetime.now(UTC)
    book = _book_with_cache(
        BookMarketCache(
            book_id=uuid.uuid4(),
            lowest_delivered_price_cents=2499,
            comparison_status=ComparisonStatus.AVAILABLE,
            checked_at=now,
            expires_at=now + timedelta(days=1),
        )
    )
    with (
        patch("app.bot.handlers.market_panel._panels", {}),
        patch("app.bot.handlers.market_panel.schedule_book_pricing") as schedule,
    ):
        await _open(book)
        schedule.assert_not_called()


@pytest.mark.asyncio
async def test_open_panel_without_any_cache_requests_the_first_collection():
    from unittest.mock import patch

    book = _book_with_cache(None)
    with (
        patch("app.bot.handlers.market_panel._panels", {}),
        patch("app.bot.handlers.market_panel.schedule_book_pricing") as schedule,
    ):
        await _open(book)
        schedule.assert_called_once_with(book.id, reason="market_panel")


@pytest.mark.asyncio
async def test_open_panel_does_not_duplicate_an_in_flight_collection():
    from unittest.mock import patch

    from app.services import book_market_pricing as pricing

    book = _book_with_cache(None)
    pricing._pricing_in_flight.add(book.id)
    try:
        with (
            patch("app.bot.handlers.market_panel._panels", {}),
            patch("app.services.book_market_pricing.safe_create_task") as task,
        ):
            await _open(book)
            task.assert_not_called()
    finally:
        pricing._pricing_in_flight.discard(book.id)


@pytest.mark.asyncio
async def test_successful_empty_collection_is_not_retried_on_reopen():
    from datetime import UTC, datetime
    from unittest.mock import patch

    from app.db.models.book_market_cache import BookMarketCache

    now = datetime.now(UTC)
    book = _book_with_cache(
        BookMarketCache(
            book_id=uuid.uuid4(),
            lowest_delivered_price_cents=None,
            collection_status="empty",
            checked_at=now,
            last_successful_collection_at=now,
            last_attempt_at=now,
        )
    )
    with (
        patch("app.bot.handlers.market_panel._panels", {}),
        patch("app.bot.handlers.market_panel.schedule_book_pricing") as schedule,
    ):
        await _open(book)
        await _open(book)
        schedule.assert_not_called()


@pytest.mark.asyncio
async def test_recent_failure_respects_the_rest_but_an_old_failure_can_retry():
    from datetime import UTC, datetime, timedelta
    from unittest.mock import patch

    from app.core.constants import ComparisonStatus
    from app.db.models.book_market_cache import BookMarketCache

    now = datetime.now(UTC)
    recent = _book_with_cache(
        BookMarketCache(
            book_id=uuid.uuid4(),
            lowest_delivered_price_cents=None,
            comparison_status=ComparisonStatus.UNAVAILABLE,
            collection_status="failed",
            last_attempt_at=now - timedelta(hours=1),
        )
    )
    old = _book_with_cache(
        BookMarketCache(
            book_id=uuid.uuid4(),
            lowest_delivered_price_cents=None,
            comparison_status=ComparisonStatus.UNAVAILABLE,
            collection_status="failed",
            last_attempt_at=now - timedelta(hours=7),
        )
    )
    with (
        patch("app.bot.handlers.market_panel._panels", {}),
        patch("app.bot.handlers.market_panel.schedule_book_pricing") as schedule,
    ):
        await _open(recent)
        schedule.assert_not_called()
        await _open(old)
        schedule.assert_called_once_with(old.id, reason="market_panel")


@pytest.mark.asyncio
async def test_an_explicit_tome_without_a_catalogue_match_is_not_replaced():
    from unittest.mock import patch

    from app.services.tome_extractor import TomeMatchResult

    first = _book_with_cache(None)
    first.title = "Série Tome 1"
    third = _book_with_cache(None)
    third.title = "Série Tome 3"
    with (
        patch("app.bot.handlers.market_panel._panels", {}),
        patch("app.bot.handlers.market_panel.schedule_book_pricing") as schedule,
    ):
        text = await _open(
            None,
            books=[first, third],
            tome_result=TomeMatchResult(is_lot=False, tome_number=2, confidence="high"),
        )
        schedule.assert_not_called()
    assert "Tome 2 détecté" in text
    assert "14,00 €" not in text
    assert "21,00 €" not in text


@pytest.mark.asyncio
async def test_a_missing_catalogue_is_resolved_once_from_the_panel():
    from unittest.mock import patch

    from app.db.models.alert import Alert

    alert = Alert(id=uuid.uuid4(), name="Série", status=AlertStatus.ACTIVE, isbn=None)
    # Le livre n'a jamais été coté : la résolution catalogue reste la première étape.
    book = _book_with_cache(None)
    book.title = "Série"
    book.normalized_title = "serie"
    started: list[object] = []

    def capture(coro, name=None):
        started.append((coro, name))
        coro.close()

    with (
        patch("app.bot.handlers.market_panel._panels", {}),
        patch("app.bot.handlers.market_panel.safe_create_task", capture),
        patch("app.bot.handlers.market_panel.schedule_book_pricing"),
    ):
        await _open(book, books=[], alert=alert)
        await _open(book, books=[], alert=alert)
        assert len(started) == 1
    market_panel._catalogue_in_flight.discard(alert.id)


@pytest.mark.asyncio
async def test_a_recent_catalogue_attempt_is_not_repeated():
    from datetime import UTC, datetime, timedelta
    from unittest.mock import patch

    from app.db.models.alert import Alert

    alert = Alert(
        id=uuid.uuid4(),
        name="Série",
        status=AlertStatus.ACTIVE,
        isbn=None,
        catalogue_refresh_attempted_at=datetime.now(UTC) - timedelta(hours=1),
    )
    started: list[object] = []

    def capture(coro, name=None):
        started.append((coro, name))
        coro.close()

    with (
        patch("app.bot.handlers.market_panel._panels", {}),
        patch("app.bot.handlers.market_panel.safe_create_task", capture),
    ):
        await _open(None, books=[], alert=alert)
        assert started == []


@pytest.mark.asyncio
async def test_catalogue_resolution_schedules_only_the_requested_tome():
    from unittest.mock import AsyncMock, patch

    from app.core.constants import ComparisonStatus
    from app.db.models.alert import Alert
    from app.db.models.book_market_cache import BookMarketCache
    from app.services.tome_extractor import TomeExtractor

    alert = Alert(id=uuid.uuid4(), name="Série", status=AlertStatus.ACTIVE, isbn=None)
    first = _book_with_cache(
        BookMarketCache(
            book_id=uuid.uuid4(),
            lowest_delivered_price_cents=None,
            comparison_status=ComparisonStatus.UNAVAILABLE,
        )
    )
    first.title = "Série Tome 1"
    second = _book_with_cache(
        BookMarketCache(
            book_id=uuid.uuid4(),
            lowest_delivered_price_cents=None,
            comparison_status=ComparisonStatus.UNAVAILABLE,
        )
    )
    second.title = "Série Tome 2"

    session = AsyncMock()
    session_ctx = AsyncMock()
    session_ctx.__aenter__.return_value = session
    session_ctx.__aexit__.return_value = False

    alert_repo = MagicMock()
    alert_repo.get_by_id = AsyncMock(return_value=alert)
    book_repo = MagicMock()
    book_repo.get_all_books_for_alert = AsyncMock(return_value=[first, second])
    use_case = MagicMock()
    use_case.resolve_alert_books = AsyncMock()

    with (
        patch("app.bot.handlers.market_panel.AsyncSessionLocal", return_value=session_ctx),
        patch("app.bot.handlers.market_panel.AlertRepository", return_value=alert_repo),
        patch("app.bot.handlers.market_panel.BookRepository", return_value=book_repo),
        patch("app.bot.handlers.market_panel.ViewMarketDetailsUseCase", return_value=use_case),
        patch("app.bot.handlers.market_panel.schedule_book_pricing") as schedule,
    ):
        await market_panel._resolve_catalogue_once(alert.id, tome_number=2)

    use_case.resolve_alert_books.assert_awaited_once_with(alert.id, force_catalogue=True)
    schedule.assert_called_once_with(second.id, reason="market_panel")
    assert TomeExtractor.extract(second.title, series_key=alert.name).tome_number == 2
    assert alert.id not in market_panel._catalogue_in_flight


@pytest.mark.asyncio
async def test_a_settled_empty_collection_does_not_reopen_catalogue_or_price():
    from datetime import UTC, datetime
    from unittest.mock import patch

    from app.db.models.alert import Alert
    from app.db.models.book_market_cache import BookMarketCache

    now = datetime.now(UTC)
    alert = Alert(id=uuid.uuid4(), name="Série", status=AlertStatus.ACTIVE, isbn=None)
    book = _book_with_cache(
        BookMarketCache(
            book_id=uuid.uuid4(),
            lowest_delivered_price_cents=None,
            collection_status="empty",
            checked_at=now,
            last_successful_collection_at=now,
            last_attempt_at=now,
        )
    )
    book.title = "Série"
    book.normalized_title = "serie"
    started: list[object] = []

    def capture(coro, name=None):
        started.append((coro, name))
        coro.close()

    with (
        patch("app.bot.handlers.market_panel._panels", {}),
        patch("app.bot.handlers.market_panel.safe_create_task", capture),
        patch("app.bot.handlers.market_panel.schedule_book_pricing") as schedule,
    ):
        await _open(book, books=[], alert=alert)
        await _open(book, books=[], alert=alert)
        schedule.assert_not_called()
        assert started == []
