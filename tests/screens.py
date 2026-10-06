"""Ce qu'un écran a publié : les messages entrants, et la lecture de leurs réponses.

Un test de parcours enchaîne les handlers et regarde ce que l'utilisateur lit. Ce
fichier porte ce qui permet de le faire sans recopier la mécanique Telegram : fabriquer
le message qui déclenche un écran, retrouver celui qui vient d'être édité, relever son
texte et ses boutons. Les doubles de frontière vivent dans `tests/add_fakes.py`, le
câblage dans `tests/add_journey.py`.

Un module partagé, pas un module de test importé par un autre : ce fichier ne collecte
rien (`python_files = ["test_*.py"]`) et ne doit rien savoir des tests qui l'utilisent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from aiogram.types import Message

from tests.add_fakes import DraftState, FakeMessage


def incoming_message(texte: str) -> FakeMessage:
    """Message entrant du parcours, avec ses réponses observables."""
    message = FakeMessage()
    message.text = texte
    message.answer = AsyncMock(return_value=MagicMock(spec=Message))
    message.answer.return_value.edit_text = AsyncMock()
    return message


def message_with_recap(command: str) -> tuple[FakeMessage, MagicMock]:
    """Message `/add` et son message d'attente, tous deux observables."""
    message = FakeMessage()
    message.text = command
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()

    loading_message = MagicMock()
    loading_message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=loading_message)
    return message, loading_message


def confirm_press() -> tuple[MagicMock, MagicMock]:
    """Callback « Créer l'alerte » et le message qu'il édite, observables."""
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    callback = MagicMock()
    callback.data = "alert_create_confirm"
    callback.message = message
    callback.answer = AsyncMock()
    return callback, message


def recap_text(loading_message: MagicMock) -> str:
    """Le récap publié par le message d'attente, après ses éditions successives."""
    return loading_message.edit_text.call_args[0][0]


def edition_buttons(loading_message: MagicMock) -> list:
    """Boutons de bascule d'édition proposés par un clavier de récap."""
    keyboard = loading_message.edit_text.call_args[1]["reply_markup"]
    return [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("al_create_edition")
    ]


def work_buttons(keyboard) -> list:
    """Boutons d'œuvres homonymes proposés par un clavier de récap."""
    return [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("al_create_work")
    ]


def index_of_line(lines: list[str], prefix: str) -> int:
    """Index de la première ligne qui commence par `prefix`."""
    return next(index for index, line in enumerate(lines) if line.startswith(prefix))


async def open_isbn_edit_field(state: DraftState) -> None:
    """Ouvre le champ ISBN du menu ✏️ Modifier, comme le fait le bouton du récap."""
    from app.bot.handlers.alerts_create_confirm import handle_create_edit_choice

    menu_message = MagicMock(spec=Message)
    menu_message.edit_text = AsyncMock()
    menu_callback = MagicMock()
    menu_callback.data = "al_create_edit:isbn"
    menu_callback.message = menu_message
    menu_callback.answer = AsyncMock()

    await handle_create_edit_choice(menu_callback, state)
    assert state.data["current_edit_field"] == "isbn"
