"""Une collecte tardive ne réouvre pas un panneau fermé ou un ancien tome."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import Message

from app.bot.handlers import market_panel
from app.db.models.book import Book
from app.use_cases.view_market_details import MarketPanelData


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
