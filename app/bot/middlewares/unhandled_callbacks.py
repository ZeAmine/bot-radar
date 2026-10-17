"""Filet de sécurité : aucun clic de bouton n'est laissé sans réponse.

Un clic sur un bouton inline ouvre un « callback query » que Telegram attend de voir
acquitter. Quand aucun handler ne prend l'événement — état FSM vidé, session perdue,
bouton d'une version précédente resté dans l'historique — la requête restait sans
réponse et le bouton tournait indéfiniment, indiscernable d'un bot figé.

Le filet vit au niveau de l'``update``, et pas de ``callback_query`` : un middleware
d'observateur n'est appelé que lorsqu'un handler a **déjà** filtré l'événement, donc
il ne voit jamais le cas « personne n'a filtré », précisément celui qu'il faut
rattraper. Ici, le résultat de toute la chaîne de routeurs remonte, ``UNHANDLED``
compris.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import TelegramObject, Update

logger = logging.getLogger(__name__)

# Le bouton cesse de tourner ; la raison affichée reste courte.
STALE_CALLBACK_MESSAGE = "⏳ Ce bouton n'est plus actif. Relance la commande."


class UnconsumedCallbackMiddleware(BaseMiddleware):
    """Acquitte un callback query qu'aucun handler n'a pris en charge."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        result = await handler(event, data)
        if result is UNHANDLED and isinstance(event, Update):
            callback = event.callback_query
            if callback is not None:
                logger.info("Callback non pris en charge, acquitté : %s", callback.data)
                await callback.answer(STALE_CALLBACK_MESSAGE)
        return result
