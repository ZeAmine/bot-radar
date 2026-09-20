"""Tests unitaires pour l'Auto-Tuning, les variantes orthographiques et le bouton dressing."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.alerts_create import handle_create_ai_variants
from app.bot.handlers.autotune import (
    _pending_tuning,
    _pending_variants,
    handle_autotune_command,
    handle_tune_apply,
    handle_var_apply,
)
from app.bot.handlers.callbacks import handle_bundle_dressing_disabled
from app.db.models.alert import Alert
from app.db.models.user import User


@pytest.mark.asyncio
async def test_bundle_dressing_disabled_callback():
    """Vérifie que le clic sur le bouton dressing affiche une popup d'indisponibilité."""
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()

    await handle_bundle_dressing_disabled(callback)

    callback.answer.assert_called_once()
    args, kwargs = callback.answer.call_args
    assert "désactivé" in args[0]
    assert kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_handle_create_ai_variants():
    """Vérifie l'ajout de variantes et d'exclusions dans le flow /add."""
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    state = MagicMock(spec=FSMContext)
    state.get_data = AsyncMock(
        return_value={
            "name": "One Piece",
            "include_keywords": ["One Piece"],
            "max_item_price_cents": 3500,
            "category_slug": "bd_comics",
        }
    )
    state.update_data = AsyncMock()

    with patch(
        "app.services.ai.modules.keyword_variants.keyword_variants_module.generate_variants_and_exclusions",
        new_callable=AsyncMock,
        return_value=(["onepiece", "one piece tomes"], ["figurine", "carte"]),
    ):
        await handle_create_ai_variants(callback, state)

        state.update_data.assert_called_once()
        updated_data = state.update_data.call_args[1]
        assert "onepiece" in updated_data["variants"]
        assert "one piece tomes" in updated_data["variants"]
        assert "figurine" in updated_data["exclude_keywords"]

        callback.message.edit_text.assert_called_once()
        text = callback.message.edit_text.call_args[0][0]
        assert "Variantes :" in text
        assert "onepiece" in text
        assert "Exclus :" in text
        assert "figurine" in text


@pytest.mark.asyncio
async def test_handle_autotune_no_dislikes():
    """Vérifie le message d'aide si aucun faux positif n'a encore été signalé."""
    message = MagicMock(spec=Message)
    message.text = "/autotune"
    message.answer = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()

    alert = Alert(id=uuid.uuid4(), name="One Piece", user_id=db_user.id)

    with (
        patch("app.bot.handlers.autotune.AlertRepository.list_by_user", new_callable=AsyncMock, return_value=[alert]),
        patch("app.bot.handlers.autotune.get_disliked_titles_for_alert", new_callable=AsyncMock, return_value=[]),
    ):
        await handle_autotune_command(message, db_user, db_session)

        message.answer.assert_called_once()
        text = message.answer.call_args[0][0]
        assert "Aucun faux positif signalé" in text


@pytest.mark.asyncio
async def test_handle_autotune_with_dislikes():
    """Vérifie la génération de suggestions d'exclusion par DeepSeek."""
    message = MagicMock(spec=Message)
    message.text = "/autotune"
    message.answer = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()

    alert = Alert(
        id=uuid.uuid4(),
        name="One Piece",
        user_id=db_user.id,
        exclude_keywords=["poster"],
    )

    disliked_titles = ["Figurine One Piece Luffy", "T-shirt One Piece taille M"]

    with (
        patch("app.bot.handlers.autotune.AlertRepository.list_by_user", new_callable=AsyncMock, return_value=[alert]),
        patch("app.bot.handlers.autotune.get_disliked_titles_for_alert", new_callable=AsyncMock, return_value=disliked_titles),
        patch(
            "app.services.ai.modules.auto_tuning.auto_tuning_module.suggest_exclusions",
            new_callable=AsyncMock,
            return_value=(["figurine", "t-shirt"], "Termes parasites fréquents"),
        ),
    ):
        await handle_autotune_command(message, db_user, db_session)

        message.answer.assert_called_once()
        text = message.answer.call_args[0][0]
        assert "Auto-Tuning : « One Piece »" in text
        assert "figurine, t-shirt" in text
        assert str(alert.id) in _pending_tuning


@pytest.mark.asyncio
async def test_handle_tune_apply():
    """Vérifie l'application effective des exclusions à l'alerte."""
    alert_id = uuid.uuid4()
    _pending_tuning[str(alert_id)] = ["figurine", "t-shirt"]

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ai_tune_apply:{alert_id}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()
    db_session.commit = AsyncMock()

    alert = Alert(
        id=alert_id,
        name="One Piece",
        user_id=db_user.id,
        exclude_keywords=["poster"],
    )

    with patch("app.bot.handlers.autotune.AlertRepository.get_by_id", new_callable=AsyncMock, return_value=alert):
        await handle_tune_apply(callback, db_user, db_session)

        assert "poster" in alert.exclude_keywords
        assert "figurine" in alert.exclude_keywords
        assert "t-shirt" in alert.exclude_keywords
        db_session.commit.assert_called_once()

        callback.answer.assert_called_once()
        callback.message.edit_text.assert_called_once()


@pytest.mark.asyncio
async def test_handle_var_apply():
    """Vérifie l'application effective des variantes orthographiques à l'alerte."""
    alert_id = uuid.uuid4()
    _pending_variants[str(alert_id)] = ["onepiece", "one piece tomes"]

    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ai_var_apply:{alert_id}"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123456)
    db_session = MagicMock()
    db_session.commit = AsyncMock()

    alert = Alert(
        id=alert_id,
        name="One Piece",
        user_id=db_user.id,
        include_keywords=["One Piece"],
    )

    with patch("app.bot.handlers.autotune.AlertRepository.get_by_id", new_callable=AsyncMock, return_value=alert):
        await handle_var_apply(callback, db_user, db_session)

        assert "One Piece" in alert.include_keywords
        assert "onepiece" in alert.include_keywords
        assert "one piece tomes" in alert.include_keywords
        db_session.commit.assert_called_once()

        callback.answer.assert_called_once()
        callback.message.edit_text.assert_called_once()
