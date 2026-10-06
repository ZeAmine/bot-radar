"""Tests unitaires pour le middleware d'authentification AuthMiddleware."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Message

from app.bot.middlewares.auth import AuthMiddleware
from app.core.config import settings


@pytest.mark.asyncio
async def test_auth_middleware_unauthorized_message(monkeypatch: pytest.MonkeyPatch):
    """Vérifie le rejet strict d'un message provenant d'un utilisateur non autorisé."""
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [11111111])
    middleware = AuthMiddleware()

    handler = AsyncMock()
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()

    tg_user = MagicMock()
    tg_user.id = 99999999  # Non autorisé

    data = {"event_from_user": tg_user}

    result = await middleware(handler, message, data)

    assert result is None
    handler.assert_not_called()
    message.answer.assert_called_once_with(AuthMiddleware.UNAUTHORIZED_MESSAGE)


@pytest.mark.asyncio
async def test_auth_middleware_unauthorized_callback(monkeypatch: pytest.MonkeyPatch):
    """Vérifie le rejet d'une action inline provenant d'un utilisateur non autorisé."""
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [11111111])
    middleware = AuthMiddleware()

    handler = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()

    tg_user = MagicMock()
    tg_user.id = 99999999

    data = {"event_from_user": tg_user}

    result = await middleware(handler, callback, data)

    assert result is None
    handler.assert_not_called()
    callback.answer.assert_called_once_with(AuthMiddleware.UNAUTHORIZED_MESSAGE, show_alert=True)


@pytest.mark.asyncio
async def test_auth_middleware_authorized_user(monkeypatch: pytest.MonkeyPatch):
    """Vérifie l'accès accordé à un utilisateur autorisé et la transmission au handler."""
    allowed_id = 11111111
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [allowed_id])
    middleware = AuthMiddleware()

    handler = AsyncMock(return_value="handler_response")
    message = MagicMock()

    tg_user = MagicMock()
    tg_user.id = allowed_id
    tg_user.username = "batman"
    tg_user.first_name = "Bruce"

    data = {"event_from_user": tg_user}

    mock_db_user = MagicMock()
    mock_db_user.telegram_user_id = allowed_id

    with (
        patch("app.bot.middlewares.auth.AsyncSessionLocal") as mock_session_local,
        patch("app.bot.middlewares.auth.UserRepository") as mock_repo_cls,
    ):
        mock_session = AsyncMock()
        mock_session_local.return_value.__aenter__.return_value = mock_session

        mock_repo = MagicMock()
        mock_repo.get_or_create = AsyncMock(return_value=mock_db_user)
        mock_repo_cls.return_value = mock_repo

        result = await middleware(handler, message, data)

        assert result == "handler_response"
        handler.assert_called_once_with(message, data)
        assert data["db_user"] == mock_db_user
        mock_repo.get_or_create.assert_called_once_with(
            telegram_user_id=allowed_id,
            username="batman",
            first_name="Bruce",
            is_allowed=True,
        )
