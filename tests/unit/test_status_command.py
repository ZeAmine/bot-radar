"""Tests unitaires pour la commande /status du bot Telegram et les notifications d'état."""

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Chat, Message

from app.bot.handlers.status import handle_status
from app.providers.llm.base import LLMHealth
from app.services.telegram_service import notify_admins


@pytest.mark.parametrize(("data", "label"), [
    (None, "Inconnu"), ({"status": "stopped"}, "Arrêté"),
    ({"status": "unexpected"}, "Inconnu"),
    ({"status": "active"}, "Inconnu"),
    ({"status": "active", "last_poll_at": time.time() + 1000}, "Inconnu"),
    ({"status": "cooldown", "cooldown_until": time.time() - 60}, "reprise non confirmée"),
    *[({"status": "active", "last_poll_at": value}, "Inconnu")
      for value in (0, None, True, "bad", float("nan"), float("inf"), 10 ** 500)],
])
async def test_poller_status_never_claims_active_without_recent_evidence(data, label):
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    with (
        patch("app.bot.handlers.status.is_night_time", return_value=False),
        patch("app.bot.handlers.status.settings.VINTED_POLLER_ENABLED", True),
        patch("app.bot.handlers.status.get_poller_status", AsyncMock(return_value=data)),
    ):
        await handle_status(message, _two_query_db([], []))
    text = message.answer.await_args.args[0]
    assert label in text
    assert "🟢 <b>Active</b>" not in text


def _two_query_db(latency_rows: list[tuple], category_rows: list[tuple]):
    """Session double qui répond à deux requêtes distinctes, dans l'ordre du handler.

    `/status` interroge désormais la base deux fois : les percentiles de latence de
    notification, puis les comptages par catégorie. Un double qui renvoyait le même
    jeu de résultats aux deux ne décrivait plus rien : la première requête recevait
    des tuples de catégorie et échouait au dépaquetage.
    """
    latency_result = MagicMock()
    latency_result.all.return_value = latency_rows
    category_result = MagicMock()
    category_result.all.return_value = category_rows
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[latency_result, category_result])
    return db


@pytest.mark.asyncio
async def test_handle_status_day_active():
    """Vérifie le message de statut quand la surveillance est active en journée."""
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()

    now_ts = time.time()
    mock_db = _two_query_db(
        # 0,98 s en p50 puis 4,09 s : les valeurs relevées en production, pour que le
        # rendu de la section « Latence » soit vérifié sur autre chose que du vide.
        latency_rows=[
            # (notified_at, received_at) : l'ordre de la requête.
            (now_ts, now_ts - 0.98),
            (now_ts, now_ts - 4.09),
        ],
        category_rows=[
            ("bd_comics", "Livres", "📚", 5),
            ("tech", "Électronique", "💻", 3),
            ("gaming", "Jeux Vidéo", "🎮", 2),
        ],
    )

    mock_status_data = {
        "status": "active",
        "last_poll_at": now_ts - 15,
        "consecutive_blocks": 0,
        # Cadence mesurée en production : 17,4 s en p50, 35,1 s au p90.
        "cycle_gap_p50_s": 17.4,
        "cycle_gap_p90_s": 35.1,
    }

    with (
        patch("app.bot.handlers.status.is_night_time", return_value=False),
        patch("app.bot.handlers.status.get_poller_status",
              new_callable=AsyncMock, return_value=mock_status_data),
        patch("app.bot.handlers.status.settings.VINTED_POLLER_ENABLED", True),
        patch("app.bot.handlers.status.settings.NIGHT_MODE_ENABLED", True),
        patch("app.bot.handlers.status.settings.NIGHT_MODE_START_HOUR", 0),
        patch("app.bot.handlers.status.settings.NIGHT_MODE_END_HOUR", 7),
    ):
        await handle_status(message, mock_db)

        message.answer.assert_called_once()
        text = message.answer.call_args[0][0]
        assert "État du service" in text
        assert "<b>Scanner Vinted :</b> 🟢 <b>Active</b>" in text
        assert "scan -" in text
        assert "00:00 - 07:00" in text
        assert "<b>Alertes actives (10) :</b>" in text
        # La section Latence rend les deux mesures (contrôlable + cadence), percentiles interpolés.
        assert "<b>Latence</b>" in text
        assert "<b>Réception → marquage :</b> 2535 ms" in text
        assert "<b>Cadence de scrutation :</b> 17.4 s" in text
        assert "📚 <b>Livres :</b> 5" in text
        assert "💻 <b>Électronique :</b> 3" in text
        assert "🎮 <b>Jeux Vidéo :</b> 2" in text
        assert "Disponibilité scan" not in text
        assert "Recherches perso" not in text
        assert "Requêtes Vinted (60 s)" not in text


@pytest.mark.asyncio
async def test_handle_status_renders_scan_observability():
    """Les champs d'observabilité du poller alimentent /status quand ils existent."""
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()

    now_ts = time.time()
    mock_db = _two_query_db(latency_rows=[], category_rows=[])

    mock_status_data = {
        "status": "active",
        "last_poll_at": now_ts - 15,
        "consecutive_blocks": 0,
        "cycle_gap_p50_s": 17.4,
        "availability": {
            "window_s": 86400.0,
            "scanned_s": 60480.0,
            "night_s": 25200.0,
            "blocked_s": 720.0,
            "network_error_s": 0.0,
            "unexpected_error_s": 0.0,
        },
        "search_stats": {"count": 3, "worst_age_s": 42.0},
        "requests_60s": {"api": 5, "html": 1, "mint": 0},
    }

    with (
        patch("app.bot.handlers.status.is_night_time", return_value=False),
        patch("app.bot.handlers.status.get_poller_status",
              new_callable=AsyncMock, return_value=mock_status_data),
        patch("app.bot.handlers.status.settings.VINTED_POLLER_ENABLED", True),
    ):
        await handle_status(message, mock_db)

    text = message.answer.call_args[0][0]
    assert (
        "<b>Disponibilité scan :</b> 70 % (24 h 00) · bloqué 12 min · nuit 7 h 00"
        in text
    )
    assert "<b>Recherches perso :</b> 3 · plus ancien scan 42 s" in text
    assert "<b>Requêtes Vinted (60 s) :</b> API 5 · HTML 1 · mint 0" in text


@pytest.mark.asyncio
async def test_handle_status_cooldown():
    """Vérifie le message de statut quand le poller est en pause de sécurité (anti-blocage)."""
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()

    mock_db = _two_query_db(
        latency_rows=[],
        category_rows=[("bd_comics", "Livres", "📚", 1)],
    )

    now_ts = time.time()
    mock_status_data = {
        "status": "cooldown",
        "consecutive_blocks": 2,
        "cooldown_until": now_ts + 250,  # 4 min 10s
        "reason": "Blocage Cloudflare (403)",
        "resume_time": "11:35",
    }

    with (
        patch("app.bot.handlers.status.is_night_time", return_value=False),
        patch("app.bot.handlers.status.get_poller_status",
              new_callable=AsyncMock, return_value=mock_status_data),
        patch("app.bot.handlers.status.settings.VINTED_POLLER_ENABLED", True),
        patch("app.bot.handlers.status.settings.NIGHT_MODE_ENABLED", True),
    ):
        await handle_status(message, mock_db)

        message.answer.assert_called_once()
        text = message.answer.call_args[0][0]
        assert "En pause anti-blocage (palier #2)" in text
        assert "<b>Motif :</b> Blocage Cloudflare (403)" in text
        assert "<b>Reprise dans :</b> 4 min" in text
        assert "à 11:35" in text


@pytest.mark.asyncio
async def test_handle_status_night_mode():
    """Vérifie le message de statut quand le mode nuit est actif."""
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()

    mock_db = _two_query_db(latency_rows=[], category_rows=[])

    with (
        patch("app.bot.handlers.status.is_night_time", return_value=True),
        patch("app.bot.handlers.status.get_poller_status",
              new_callable=AsyncMock, return_value=None),
        patch("app.bot.handlers.status.settings.NIGHT_MODE_ENABLED", True),
        patch("app.bot.handlers.status.settings.NIGHT_MODE_START_HOUR", 0),
        patch("app.bot.handlers.status.settings.NIGHT_MODE_END_HOUR", 7),
    ):
        await handle_status(message, mock_db)

        message.answer.assert_called_once()
        text = message.answer.call_args[0][0]
        assert "<b>Scanner Vinted :</b> <b>Mode nuit</b>" in text
        assert "Aucune alerte active" in text


def _receipt(chat_id: int) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
    )


@pytest.mark.asyncio
async def test_notify_admins():
    """Vérifie la diffusion d'un message aux utilisateurs autorisés."""
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=[_receipt(123456), _receipt(789012)]
    )

    with patch("app.services.telegram_service.settings.TELEGRAM_ALLOWED_USER_IDS", [123456,
        789012]):
        accepted = await notify_admins("Test alert message", bot=bot)

        assert accepted is True
        assert bot.send_message.call_count == 2
        bot.send_message.assert_any_call(
            chat_id=123456, text="Test alert message", parse_mode="HTML")
        bot.send_message.assert_any_call(
            chat_id=789012, text="Test alert message", parse_mode="HTML")


@pytest.mark.asyncio
async def test_notify_admins_failure_is_not_accepted():
    """Un échec d'envoi ne doit jamais être présenté comme accepté."""
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("transport indisponible"))

    with patch("app.services.telegram_service.settings.TELEGRAM_ALLOWED_USER_IDS", [123456]):
        accepted = await notify_admins("Test alert message", bot=bot)

        assert accepted is False


@pytest.mark.asyncio
async def test_handle_status_ai_active():
    """Vérifie l'affichage du statut IA actif dans /status."""
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()

    mock_db = _two_query_db(latency_rows=[], category_rows=[])

    mock_ai = LLMHealth(
        status="active",
        model="un-modele-choisi",
        latency_ms=115,
        message="Opérationnel (115ms)",
    )

    with (
        patch("app.bot.handlers.status.is_night_time", return_value=False),
        patch("app.bot.handlers.status.get_poller_status",
              new_callable=AsyncMock, return_value=None),
        patch("app.bot.handlers.status.llm_client.check_health",
              new_callable=AsyncMock, return_value=mock_ai),
    ):
        await handle_status(message, mock_db)

        message.answer.assert_called_once()
        text = message.answer.call_args[0][0]
        assert "<b>Moteur IA</b> (sonde) : 🟢 <b>Sonde accessible</b>" in text
        assert "un-modele-choisi" not in text
        assert "latence" not in text
        assert "115ms" not in text


@pytest.mark.asyncio
async def test_handle_status_ai_error():
    """Vérifie l'affichage du statut IA en erreur dans /status."""
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()

    mock_db = _two_query_db(latency_rows=[], category_rows=[])

    mock_ai = LLMHealth(
        status="error",
        model="un-modele-choisi",
        latency_ms=None,
        message="Clé API invalide ou refusée",
    )

    with (
        patch("app.bot.handlers.status.is_night_time", return_value=False),
        patch("app.bot.handlers.status.get_poller_status",
              new_callable=AsyncMock, return_value=None),
        patch("app.bot.handlers.status.llm_client.check_health",
              new_callable=AsyncMock, return_value=mock_ai),
    ):
        await handle_status(message, mock_db)

        message.answer.assert_called_once()
        text = message.answer.call_args[0][0]
        assert (
            "<b>Moteur IA</b> (sonde) : 🔴 <b>Indisponible</b> "
            "(Clé API invalide ou refusée)"
            in text
        )


def test_health_command_removed():
    """Vérifie que la commande /health a bien été retirée des commandes Telegram et de l'aide."""
    from app.bot.bot_instance import BOT_COMMANDS
    from app.bot.handlers.help import HELP_TEXT
    from app.bot.handlers.status import router

    command_names = [cmd.command for cmd in BOT_COMMANDS]
    assert "health" not in command_names
    # /status reste vivant hors menu et figure dans /help (ne pas le retirer).
    assert "/health" not in HELP_TEXT
    assert "/status" in HELP_TEXT

    # Vérifie qu'aucun handler n'écoute sur Command("health")
    for handler in router.message.handlers:
        for filter_obj in handler.filters:
            commands = getattr(filter_obj, "commands", None)
            if commands:
                assert "health" not in commands

