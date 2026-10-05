"""Tests unitaires pour le handler /stats et ses callbacks interactifs."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message

from app.bot.handlers.stats import handle_stats, handle_stats_callback


@pytest.mark.asyncio
async def test_handle_stats_command():
    """Vérifie la réponse à la commande /stats."""
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()

    mock_db = MagicMock()

    with pytest.MonkeyPatch.context() as mp:
        mock_service = MagicMock()
        mock_service.get_formatted_stats = AsyncMock(return_value="📊 Statistiques (7 jours)")
        mp.setattr("app.bot.handlers.stats.StatisticsService", lambda _s: mock_service)

        await handle_stats(message, mock_db)

        message.answer.assert_called_once()
        args, kwargs = message.answer.call_args
        assert kwargs["text"] == "📊 Statistiques (7 jours)"
        assert "reply_markup" in kwargs
        keyboard = kwargs["reply_markup"].inline_keyboard
        assert len(keyboard) == 1
    assert "✅ 7 jours" in keyboard[0][0].text
    assert keyboard[0][1].text == "30 jours"


@pytest.mark.asyncio
async def test_handle_stats_callback_switch_period():
    """Vérifie le basculement dynamique de période vers 30 jours via callback inline."""
    callback = MagicMock()
    callback.data = "stats:30"
    callback.answer = AsyncMock()

    inner_message = MagicMock(spec=Message)
    inner_message.edit_text = AsyncMock()
    callback.message = inner_message

    mock_db = MagicMock()

    with pytest.MonkeyPatch.context() as mp:
        mock_service = MagicMock()
        mock_service.get_formatted_stats = AsyncMock(return_value="📊 Statistiques (30 jours)")
        mp.setattr("app.bot.handlers.stats.StatisticsService", lambda _s: mock_service)

        await handle_stats_callback(callback, mock_db)

        mock_service.get_formatted_stats.assert_called_once_with(days=30)
        inner_message.edit_text.assert_called_once()
        args, kwargs = inner_message.edit_text.call_args
        assert kwargs["text"] == "📊 Statistiques (30 jours)"
        keyboard = kwargs["reply_markup"].inline_keyboard
        assert keyboard[0][0].text == "7 jours"
        assert "✅ 30 jours" in keyboard[0][1].text
        callback.answer.assert_called_once()
