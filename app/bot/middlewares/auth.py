"""Middleware aiogram d'authentification et de contrôle d'accès privé."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser

from app.core.security import is_telegram_user_allowed
from app.db.repositories.user_repo import UserRepository
from app.db.session import AsyncSessionLocal


class AuthMiddleware(BaseMiddleware):
    """Middleware vérifiant que l'utilisateur Telegram est expressément autorisé."""

    UNAUTHORIZED_MESSAGE = "⛔ Ce bot est privé."

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user: TgUser | None = data.get("event_from_user")
        if not from_user:
            return await handler(event, data)

        if not is_telegram_user_allowed(from_user.id):
            if isinstance(event, Message):
                await event.answer(self.UNAUTHORIZED_MESSAGE)
            elif isinstance(event, CallbackQuery):
                await event.answer(self.UNAUTHORIZED_MESSAGE, show_alert=True)
            return None

        async with AsyncSessionLocal() as session:
            user_repo = UserRepository(session)
            db_user = await user_repo.get_or_create(
                telegram_user_id=from_user.id,
                username=from_user.username,
                first_name=from_user.first_name,
                is_allowed=True,
            )
            await session.commit()
            data["db_user"] = db_user
            data["db_session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise
