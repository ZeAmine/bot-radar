"""Routeur partagé du flux de création d'alertes."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, TelegramObject

STALE_CREATION_MESSAGE = (
    "ℹ️ Cette création est terminée. Utilise /add pour créer une nouvelle alerte."
)


def _creation_action(data: str) -> bool:
    return data.startswith(("alert_create_", "al_create_", "cond_", "add_cat:", "upd_alert_price:"))


def bind_creation_keyboard(
    keyboard: InlineKeyboardMarkup, token: str | None,
) -> InlineKeyboardMarkup:
    """Lie les boutons au brouillon ; les brouillons historiques gardent leur format."""
    if not token:
        return keyboard
    rows = []
    for row in keyboard.inline_keyboard:
        bound = []
        for button in row:
            raw = button.callback_data
            if raw and _creation_action(raw):
                base = raw.split("@")[0]
                if base.startswith("upd_alert_price:"):
                    base = "upd_alert_price:"  # ID/prix déjà dans le FSM ; respecter les 64 octets.
                button = button.model_copy(update={"callback_data": f"{base}@{token}"})
            bound.append(button)
        rows.append(bound)
    return keyboard.model_copy(update={"inline_keyboard": rows})


class CreationDraftMiddleware(BaseMiddleware):
    """Valide le brouillon avant filtres et mutations, sans modifier l'update d'origine."""

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject, data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery) or not _creation_action(event.data or ""):
            return await handler(event, data)
        state = data.get("state")
        draft = await state.get_data() if state is not None else {}
        expected = draft.get("creation_token")
        base, separator, received = (event.data or "").partition("@")
        if (expected and (not separator or received != expected)) or (separator and not expected):
            await event.answer(STALE_CREATION_MESSAGE)
            return None
        if separator:
            event = event.model_copy(update={"data": base})
        return await handler(event, data)

router = Router(name="alerts_create")
router.callback_query.outer_middleware(CreationDraftMiddleware())
