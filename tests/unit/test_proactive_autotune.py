"""Tests unitaires pour l'Auto-Tuning proactif et le self-tuning (Phase A)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.autotune import handle_tune_dismiss
from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.user import User
from app.services.ai.modules.auto_tuning import (
    get_pending_tuning,
    is_autotune_on_cooldown,
    pop_pending_tuning,
    save_pending_tuning,
    set_autotune_cooldown,
)
from app.tasks.proactive_autotune import (
    check_and_trigger_proactive_autotune,
    run_proactive_autotune_scan,
    schedule_proactive_autotune_check,
)


@pytest.mark.asyncio
async def test_pending_tuning_cache_lifecycle():
    """Vérifie l'enregistrement, la récupération et l'extraction des exclusions en attente."""
    alert_id = uuid.uuid4()
    exclusions = ["dvd", "blu-ray", "figurine"]

    # Sauvegarde
    await save_pending_tuning(alert_id, exclusions)

    # Lecture
    retrieved = await get_pending_tuning(alert_id)
    assert retrieved == exclusions

    # Extraction (pop)
    popped = await pop_pending_tuning(alert_id)
    assert popped == exclusions

    # Vérification que le cache est vide après pop
    empty = await get_pending_tuning(alert_id)
    assert empty == []


@pytest.mark.asyncio
async def test_autotune_cooldown():
    """Vérifie le fonctionnement du cooldown anti-spam."""
    alert_id = uuid.uuid4()
    with patch("app.services.ai.modules.auto_tuning.get_redis_client") as mock_redis_factory:
        mock_redis = MagicMock()
        mock_redis.exists = AsyncMock(return_value=1)
        mock_redis.set = AsyncMock()
        mock_redis_factory.return_value = mock_redis

        await set_autotune_cooldown(alert_id, ttl_seconds=3600)
        mock_redis.set.assert_awaited_once_with(f"autotune:cooldown:{alert_id}", "1", ex=3600)

        in_cooldown = await is_autotune_on_cooldown(alert_id)
        assert in_cooldown is True


@pytest.mark.asyncio
async def test_check_and_trigger_proactive_autotune_success():
    """Vérifie qu'une alerte avec >=2 faux positifs reçoit une proposition Telegram proactive."""
    alert_id = uuid.uuid4()
    alert = Alert(
        id=alert_id,
        name="Harry Potter Intégrale",
        status=AlertStatus.ACTIVE,
        exclude_keywords=["cassette"],
    )

    bot = MagicMock()
    bot.send_message = AsyncMock()

    mock_session = AsyncMock()
    mock_alert_repo = MagicMock()
    mock_alert_repo.get_by_id = AsyncMock(return_value=alert)

    with (
        patch(
            "app.tasks.proactive_autotune.is_autotune_on_cooldown",
            AsyncMock(return_value=False),
        ),
        patch("app.tasks.proactive_autotune.AsyncSessionLocal") as mock_session_ctx,
        patch("app.tasks.proactive_autotune.AlertRepository", return_value=mock_alert_repo),
        patch(
            "app.tasks.proactive_autotune.get_disliked_titles_for_alert",
            AsyncMock(return_value=["Harry Potter DVD Coffret", "Harry Potter Jeu PS4"]),
        ),
        patch(
            "app.tasks.proactive_autotune.auto_tuning_module.suggest_exclusions",
            AsyncMock(return_value=(["dvd", "jeu"], "Parasites détectés")),
        ),
        patch("app.tasks.proactive_autotune.save_pending_tuning", AsyncMock()) as mock_save,
        patch("app.tasks.proactive_autotune.set_autotune_cooldown", AsyncMock()) as mock_cooldown,
    ):
        mock_session_ctx.return_value.__aenter__.return_value = mock_session

        res = await check_and_trigger_proactive_autotune(alert_id, bot, chat_id=123456789)

        assert res is True
        mock_save.assert_awaited_once_with(alert_id, ["dvd", "jeu"])
        mock_cooldown.assert_awaited_once()

        bot.send_message.assert_awaited_once()
        _, kwargs = bot.send_message.call_args
        assert kwargs["chat_id"] == 123456789
        assert "Auto-Tuning Proactif" in kwargs["text"]
        assert "dvd, jeu" in kwargs["text"]

        # Vérification des boutons interactifs
        kb = kwargs["reply_markup"].inline_keyboard
        assert kb[0][0].callback_data == f"ai_tune_apply:{alert_id}"
        assert kb[1][0].callback_data == f"ai_tune_edit:{alert_id}"
        assert kb[2][0].callback_data == f"ai_tune_dismiss:{alert_id}"


@pytest.mark.asyncio
async def test_check_and_trigger_proactive_autotune_skips_when_on_cooldown():
    """Vérifie qu'aucune notification n'est envoyée si l'alerte est en cooldown."""
    alert_id = uuid.uuid4()
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with patch(
        "app.tasks.proactive_autotune.is_autotune_on_cooldown",
        AsyncMock(return_value=True),
    ):
        res = await check_and_trigger_proactive_autotune(alert_id, bot, chat_id=123456789)
        assert res is False
        bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_and_trigger_proactive_autotune_skips_when_insufficient_dislikes():
    """Vérifie qu'aucune notification n'est envoyée avec moins de 2 faux positifs."""
    alert_id = uuid.uuid4()
    alert = Alert(id=alert_id, name="Tintin", status=AlertStatus.ACTIVE)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    mock_session = AsyncMock()
    mock_alert_repo = MagicMock()
    mock_alert_repo.get_by_id = AsyncMock(return_value=alert)

    with (
        patch(
            "app.tasks.proactive_autotune.is_autotune_on_cooldown",
            AsyncMock(return_value=False),
        ),
        patch("app.tasks.proactive_autotune.AsyncSessionLocal") as mock_session_ctx,
        patch("app.tasks.proactive_autotune.AlertRepository", return_value=mock_alert_repo),
        patch(
            "app.tasks.proactive_autotune.get_disliked_titles_for_alert",
            AsyncMock(return_value=["Tintin Figurine"]),
        ),
    ):
        mock_session_ctx.return_value.__aenter__.return_value = mock_session

        res = await check_and_trigger_proactive_autotune(alert_id, bot, chat_id=123456789)
        assert res is False
        bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_and_trigger_proactive_autotune_skips_when_no_new_exclusions():
    """Vérifie qu'aucune notification n'est envoyée si toutes les exclusions existent déjà."""
    alert_id = uuid.uuid4()
    alert = Alert(
        id=alert_id,
        name="Tintin",
        status=AlertStatus.ACTIVE,
        exclude_keywords=["figurine", "poster"],
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    mock_session = AsyncMock()
    mock_alert_repo = MagicMock()
    mock_alert_repo.get_by_id = AsyncMock(return_value=alert)

    with (
        patch(
            "app.tasks.proactive_autotune.is_autotune_on_cooldown",
            AsyncMock(return_value=False),
        ),
        patch("app.tasks.proactive_autotune.AsyncSessionLocal") as mock_session_ctx,
        patch("app.tasks.proactive_autotune.AlertRepository", return_value=mock_alert_repo),
        patch(
            "app.tasks.proactive_autotune.get_disliked_titles_for_alert",
            AsyncMock(return_value=["Tintin Figurine", "Tintin Poster mural"]),
        ),
        patch(
            "app.tasks.proactive_autotune.auto_tuning_module.suggest_exclusions",
            AsyncMock(return_value=(["figurine", "poster"], "Déjà exclus")),
        ),
    ):
        mock_session_ctx.return_value.__aenter__.return_value = mock_session

        res = await check_and_trigger_proactive_autotune(alert_id, bot, chat_id=123456789)
        assert res is False
        bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_proactive_autotune_scan():
    """Vérifie l'exécution du scan périodique pour APScheduler."""
    user = User(telegram_user_id=987654321)
    alert = Alert(id=uuid.uuid4(), name="Spiderman", status=AlertStatus.ACTIVE)
    bot = MagicMock()

    with (
        patch("app.tasks.proactive_autotune.AsyncSessionLocal") as mock_session_ctx,
        patch(
            "app.tasks.proactive_autotune.get_all_alerts_with_dislike_threshold",
            AsyncMock(return_value=[(alert, user, 3)]),
        ),
        patch(
            "app.tasks.proactive_autotune.check_and_trigger_proactive_autotune",
            AsyncMock(return_value=True),
        ) as mock_trigger,
        patch("app.tasks.proactive_autotune.asyncio.sleep", AsyncMock()),
    ):
        mock_session_ctx.return_value.__aenter__.return_value = AsyncMock()

        count = await run_proactive_autotune_scan(bot)
        assert count == 1
        mock_trigger.assert_awaited_once_with(
            alert_id=alert.id,
            bot=bot,
            chat_id=987654321,
        )


@pytest.mark.asyncio
async def test_handle_tune_dismiss_callback():
    """Vérifie le traitement du bouton d'annulation/rejet [ ❌ Ignorer ]."""
    alert_id = uuid.uuid4()
    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"ai_tune_dismiss:{alert_id}"
    callback.answer = AsyncMock()
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()

    with patch("app.bot.handlers.autotune.pop_pending_tuning", AsyncMock()) as mock_pop:
        await handle_tune_dismiss(callback)
        mock_pop.assert_awaited_once_with(alert_id)
        callback.answer.assert_awaited_once_with("Suggestion ignorée.")
        callback.message.edit_text.assert_awaited_once()
        args, _ = callback.message.edit_text.call_args
        assert "Suggestion d'Auto-Tuning ignorée" in args[0]


def test_schedule_proactive_autotune_check_launches_task():
    """Vérifie que la fonction synchrone lance une tâche asynchrone non-bloquante."""
    alert_id = uuid.uuid4()
    bot = MagicMock()

    with patch("app.tasks.proactive_autotune.safe_create_task") as mock_safe_task:
        schedule_proactive_autotune_check(alert_id, bot, chat_id=123)
        mock_safe_task.assert_called_once()
        coro = mock_safe_task.call_args[0][0]
        coro.close()
