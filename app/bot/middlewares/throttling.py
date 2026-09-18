"""Middleware aiogram anti-flood et limitation de débit utilisateur."""

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser

from app.api.dependencies import get_redis_pool
from app.core.rate_limiter import SlidingWindowRateLimiter

logger = logging.getLogger("radar.bot.throttling")


class ThrottlingMiddleware(BaseMiddleware):
    """Middleware vérifiant la fréquence des requêtes par utilisateur (chat/ID)."""

    def __init__(
        self,
        rate_limit_per_second: int = 5,
        window_seconds: int = 3,
        limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        super().__init__()
        self.rate_limit = rate_limit_per_second
        self.window_seconds = window_seconds
        self.limiter = limiter or SlidingWindowRateLimiter(redis_pool_factory=get_redis_pool)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user: TgUser | None = data.get("event_from_user")
        if not from_user:
            return await handler(event, data)

        limiter = self.limiter

        rate_limit_key = f"rl:tg:user:{from_user.id}"
        allowed, _, retry_after = await limiter.check_rate_limit(
            key=rate_limit_key,
            max_requests=self.rate_limit,
            window_seconds=self.window_seconds,
        )

        if not allowed:
            logger.warning(
                "Utilisateur Telegram %d bloqué temporairement (attente: %ds)",
                from_user.id,
                retry_after,
            )
            if isinstance(event, CallbackQuery):
                with contextlib.suppress(Exception):
                    await event.answer("⏳ Doucement ! Merci de patienter un instant...", show_alert=False)
            elif isinstance(event, Message):
                with contextlib.suppress(Exception):
                    await event.answer("⏳ Envoi trop rapide. Merci de patienter un instant...")
            return None

        return await handler(event, data)
