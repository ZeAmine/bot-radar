"""Routeur partagé du flux de création d'alertes."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, TelegramObject

from app.bot.creation_drafts import draft_guard
from app.bot.states.alert_creation import AlertCreationState

logger = logging.getLogger(__name__)

STALE_CREATION_MESSAGE = (
    "ℹ️ Cette création est terminée. Utilise /add pour créer une nouvelle alerte."
)
UNKNOWN_CREATION_MESSAGE = (
    "ℹ️ Résultat de création incertain ; ce brouillon ne peut pas être rejoué."
)


def creation_callback_matches(raw: str, draft: dict[str, Any]) -> bool:
    """Compare l'identité serveur sans consommer ni modifier le callback."""
    expected = draft.get("creation_token")
    _base, separator, received = raw.partition("@")
    return bool(received == expected if expected else not separator)


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
                    base = "upd_alert_price:"  # Valeur déjà en FSM ; limite Telegram : 64 octets.
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
        action = (event.data or "").split("@")[0]
        if state is not None and action in {"alert_create_confirm", "alert_create_cancel"}:
            preliminary = await state.get_data()
            if (
                creation_callback_matches(event.data or "", preliminary)
                and not preliminary.get("creation_outcome")
                and (
                    action == "alert_create_cancel"
                    or await state.get_state() == AlertCreationState.confirm_creation.state
                )
            ):
                await event.answer()  # Acquittement seul, hors de la protection métier.
            async with draft_guard(state) as attempt:
                draft = await state.get_data()
                # Le middleware FSM a pu lire l'état avant l'attente du verrou : les filtres
                # doivent décider sur l'état relu, y compris sans nonce historique.
                data["raw_state"] = await state.get_state()
                if (
                    action == "alert_create_confirm"
                    and data["raw_state"] != AlertCreationState.confirm_creation.state
                ):
                    attempt.release()
                    await event.answer(STALE_CREATION_MESSAGE)
                    return None
                if draft.get("creation_outcome") in {"unknown", "committed"}:
                    message = (
                        UNKNOWN_CREATION_MESSAGE
                        if draft["creation_outcome"] == "unknown" else STALE_CREATION_MESSAGE
                    )
                    attempt.release()
                    await event.answer(message)
                    return None
                data["creation_attempt"] = attempt
                try:
                    return await self._dispatch(handler, event, data)
                except Exception as exc:
                    logger.warning(
                        "Création interrompue : effet=%s erreur=%s",
                        attempt.effect, type(exc).__name__,
                    )
                    raise
        return await self._dispatch(handler, event, data)

    async def _dispatch(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery, data: dict[str, Any],
    ) -> Any:
        state = data.get("state")
        base, separator, received = (event.data or "").partition("@")
        draft = await state.get_data() if state is not None else {}
        if not creation_callback_matches(event.data or "", draft):
            attempt = data.get("creation_attempt")
            if attempt is not None:
                attempt.release()
            await event.answer(STALE_CREATION_MESSAGE)
            return None
        if separator:
            event = event.model_copy(update={"data": base})
        if base.startswith("add_cat:"):
            data["creation_callback_data"] = base + (f"@{received}" if separator else "")
        return await handler(event, data)

router = Router(name="alerts_create")
router.callback_query.outer_middleware(CreationDraftMiddleware())
