"""Interactions sur l'annonce et isolation des tests du vrai Telegram."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import SendMessage
from aiogram.types import Message

from app.bot.handlers.callbacks import handle_dislike
from app.bot.keyboards.alerts import get_alert_edit_fields_keyboard
from app.db.models.listing import Listing


def test_edit_menu_exposes_market_only_for_books_without_ai_actions():
    for category in ("bd_comics", "tech", "clothes", "gaming"):
        keyboard = get_alert_edit_fields_keyboard(uuid.uuid4(), category)
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        assert not any("IA" in label for label in labels)
        assert ("Prix du marché" in labels) == (category == "bd_comics")


@pytest.mark.asyncio
async def test_dislike_changes_only_the_keyboard_on_the_announcement():
    listing = Listing(
        id=uuid.uuid4(), title="Livre", url="https://vinted.fr/items/1", category_slug="bd_comics"
    )
    message = MagicMock(spec=Message)
    message.edit_reply_markup = AsyncMock()
    message.reply = AsyncMock()
    message.edit_text = AsyncMock()
    callback = MagicMock(message=message, data=f"dislike:{listing.id}")
    callback.answer = AsyncMock()
    with patch(
        "app.bot.handlers.callbacks.ListingRepository.get_by_id", AsyncMock(return_value=listing)
    ):
        await handle_dislike(callback, MagicMock())
    message.reply.assert_not_awaited()
    message.edit_text.assert_not_awaited()
    rows = message.edit_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard
    assert [b.text for b in rows[-1]] == ["Confirmer", "Annuler"]


@pytest.mark.asyncio
async def test_real_telegram_transport_is_blocked_before_opening_a_connection(monkeypatch):
    session = AiohttpSession()
    monkeypatch.setattr(session, "create_session", AsyncMock(side_effect=RuntimeError("réseau")))
    bot = Bot("123456789:AAG_ci_test_dummy_token_abc123", session=session)
    with pytest.raises(AssertionError, match="Telegram réel interdit"):
        await session.make_request(bot, SendMessage(chat_id=1, text="ne doit jamais partir"))
    await session.close()
