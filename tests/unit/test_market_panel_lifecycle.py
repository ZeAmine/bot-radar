"""Une collecte tardive ne réouvre pas un panneau fermé ou un ancien tome."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import Message

from app.bot.handlers import market_panel
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
        refreshing=False,
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


def test_missing_first_tome_does_not_hide_refresh_warning_or_date():
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
        refreshing=False,
    )
    text, _ = market_panel._content(view)
    assert "Actualisation partielle (07/10 à 02:00)" in text
    assert "Tome 1 absent du catalogue" in text
