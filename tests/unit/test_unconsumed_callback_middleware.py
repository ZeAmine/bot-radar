"""Tests du filet qui acquitte les callbacks qu'aucun handler n'a pris.

Le cas « personne n'a filtré » ne survient qu'au niveau de l'``update`` : c'est le
seul résultat qui remonte encore ``UNHANDLED``. Ces tests fixent cette frontière —
acquitter l'inconsommé, ne rien toucher de ce qui l'a été.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import CallbackQuery, Message, Update

from app.bot.middlewares.unhandled_callbacks import (
    STALE_CALLBACK_MESSAGE,
    UnconsumedCallbackMiddleware,
)


def _callback_event(data: str = "stale:1") -> tuple[Update, AsyncMock]:
    """Un Update portant un callback query, dont `answer` est espionné."""
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.answer = AsyncMock()

    event = MagicMock(spec=Update)
    event.callback_query = callback
    return event, callback.answer


@pytest.mark.asyncio
async def test_unconsumed_callback_is_acknowledged():
    """Aucun handler n'a filtré : le callback doit tout de même être acquitté."""
    middleware = UnconsumedCallbackMiddleware()
    handler = AsyncMock(return_value=UNHANDLED)
    event, answer = _callback_event()

    result = await middleware(handler, event, {})

    assert result is UNHANDLED
    answer.assert_awaited_once_with(STALE_CALLBACK_MESSAGE)


@pytest.mark.asyncio
async def test_consumed_callback_is_not_answered_twice():
    """Un handler a répondu : le filet ne doit pas ajouter une seconde réponse."""
    middleware = UnconsumedCallbackMiddleware()
    handler = AsyncMock(return_value=None)
    event, answer = _callback_event()

    await middleware(handler, event, {})

    answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_unhandled_non_callback_update_is_left_alone():
    """Un message non pris en charge n'a rien à acquitter : pas d'exception."""
    middleware = UnconsumedCallbackMiddleware()
    handler = AsyncMock(return_value=UNHANDLED)

    event = MagicMock(spec=Update)
    event.callback_query = None
    event.message = MagicMock(spec=Message)

    result = await middleware(handler, event, {})

    assert result is UNHANDLED


def test_the_net_is_registered_on_the_production_dispatcher():
    """Le filet doit être branché sur le vrai dispatcher, pas seulement dans les tests."""
    from app.bot.bot_instance import dp

    assert any(
        isinstance(middleware, UnconsumedCallbackMiddleware)
        for middleware in dp.update.outer_middleware
    ), "le filet n'est pas enregistré sur le dispatcher de production"
