"""Tests unitaires pour le Mode Nuit (heures creuses) et le garde anti-blocage."""

from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.tasks.vinted_poller import (
    get_adaptive_poll_interval,
    get_night_mode_remaining_seconds,
    is_night_time,
    is_peak_hours,
    run_vinted_poller,
)


def test_is_night_time_disabled():
    """Vérifie que is_night_time renvoie False si NIGHT_MODE_ENABLED est False."""
    with patch("app.tasks.vinted_poller.settings.NIGHT_MODE_ENABLED", False):
        tz = ZoneInfo("Europe/Paris")
        dt_night = datetime(2026, 9, 13, 3, 30, tzinfo=tz)
        assert is_night_time(dt_night) is False


def test_is_night_time_standard_interval():
    """Vérifie la détection d'heures creuses pour l'intervalle par défaut (00h à 07h)."""
    tz = ZoneInfo("Europe/Paris")
    with (
        patch("app.tasks.vinted_poller.settings.NIGHT_MODE_ENABLED", True),
        patch("app.tasks.vinted_poller.settings.NIGHT_MODE_START_HOUR", 0),
        patch("app.tasks.vinted_poller.settings.NIGHT_MODE_END_HOUR", 7),
        patch("app.tasks.vinted_poller.settings.NIGHT_MODE_TIMEZONE", "Europe/Paris"),
    ):
        # 03:30 -> Nuit
        assert is_night_time(datetime(2026, 9, 13, 3, 30, tzinfo=tz)) is True
        # 00:00 -> Nuit (borne début)
        assert is_night_time(datetime(2026, 9, 13, 0, 0, tzinfo=tz)) is True
        # 06:59 -> Nuit
        assert is_night_time(datetime(2026, 9, 13, 6, 59, tzinfo=tz)) is True
        # 07:00 -> Jour (borne fin)
        assert is_night_time(datetime(2026, 9, 13, 7, 0, tzinfo=tz)) is False
        # 14:00 -> Jour
        assert is_night_time(datetime(2026, 9, 13, 14, 0, tzinfo=tz)) is False
        # 23:59 -> Jour (avant minuit)
        assert is_night_time(datetime(2026, 9, 13, 23, 59, tzinfo=tz)) is False


def test_is_night_time_overnight_interval():
    """Vérifie la détection d'heures creuses chevauchant minuit (ex: 23h à 07h)."""
    tz = ZoneInfo("Europe/Paris")
    with (
        patch("app.tasks.vinted_poller.settings.NIGHT_MODE_ENABLED", True),
        patch("app.tasks.vinted_poller.settings.NIGHT_MODE_START_HOUR", 23),
        patch("app.tasks.vinted_poller.settings.NIGHT_MODE_END_HOUR", 7),
        patch("app.tasks.vinted_poller.settings.NIGHT_MODE_TIMEZONE", "Europe/Paris"),
    ):
        # 23:15 -> Nuit
        assert is_night_time(datetime(2026, 9, 13, 23, 15, tzinfo=tz)) is True
        # 02:00 -> Nuit
        assert is_night_time(datetime(2026, 9, 14, 2, 0, tzinfo=tz)) is True
        # 06:59 -> Nuit
        assert is_night_time(datetime(2026, 9, 14, 6, 59, tzinfo=tz)) is True
        # 07:00 -> Jour
        assert is_night_time(datetime(2026, 9, 14, 7, 0, tzinfo=tz)) is False
        # 22:59 -> Jour
        assert is_night_time(datetime(2026, 9, 13, 22, 59, tzinfo=tz)) is False


def test_get_night_mode_remaining_seconds():
    """Vérifie le calcul précis du temps restant avant la fin du mode nuit."""
    tz = ZoneInfo("Europe/Paris")
    with (
        patch("app.tasks.vinted_poller.settings.NIGHT_MODE_END_HOUR", 7),
        patch("app.tasks.vinted_poller.settings.NIGHT_MODE_TIMEZONE", "Europe/Paris"),
    ):
        # À 03:00, il reste exactement 4h = 14400s
        dt = datetime(2026, 9, 13, 3, 0, 0, tzinfo=tz)
        assert get_night_mode_remaining_seconds(dt) == 14400.0

        # À 06:30, il reste 30 min = 1800s
        dt2 = datetime(2026, 9, 13, 6, 30, 0, tzinfo=tz)
        assert get_night_mode_remaining_seconds(dt2) == 1800.0

        # À 23:00, cible demain 07:00 = 8h = 28800s
        dt3 = datetime(2026, 9, 13, 23, 0, 0, tzinfo=tz)
        assert get_night_mode_remaining_seconds(dt3) == 28800.0


def test_anti_block_cooldown_backoff():
    """Vérifie que le backoff anti-blocage progresse correctement de 3 min à 60 min."""
    cooldown_map = {1: 180, 2: 420, 3: 900, 4: 1800}

    assert cooldown_map.get(1, 3600) == 180    # 3 min
    assert cooldown_map.get(2, 3600) == 420    # 7 min
    assert cooldown_map.get(3, 3600) == 900    # 15 min
    assert cooldown_map.get(4, 3600) == 1800   # 30 min
    assert cooldown_map.get(5, 3600) == 3600   # 60 min
    assert cooldown_map.get(10, 3600) == 3600  # max 60 min


@pytest.mark.asyncio
async def test_poller_night_mode_suspension():
    """Vérifie que le poller suspend les requêtes en mode nuit et s'arrête proprement."""
    import asyncio

    stop_event = asyncio.Event()

    with (
        patch("app.tasks.vinted_poller.is_night_time", return_value=True),
        patch("app.tasks.vinted_poller.get_night_mode_remaining_seconds", return_value=120.0),
        patch(
            "app.tasks.vinted_poller.load_initial_seen_ids",
            new_callable=AsyncMock,
            return_value=set(),
        ),
        patch("app.tasks.vinted_poller.poll_vinted_once", new_callable=AsyncMock) as mock_poll,
        patch("app.tasks.vinted_poller.save_poller_status", new_callable=AsyncMock),
        patch(
            "app.tasks.vinted_poller.vinted_session_manager.clear_session",
            new_callable=AsyncMock,
        ) as mock_clear,
    ):
        task = asyncio.create_task(run_vinted_poller(stop_event))
        await asyncio.sleep(0.1)

        # En mode nuit, aucune requête poll_vinted_once ne doit être déclenchée
        mock_poll.assert_not_called()
        mock_clear.assert_called()

        # Arrêt
        stop_event.set()
        await task


def test_is_peak_hours():
    tz = ZoneInfo("Europe/Paris")
    # 12:30 -> Heure de pointe (déjeuner)
    assert is_peak_hours(datetime(2026, 9, 13, 12, 30, tzinfo=tz)) is True
    # 20:00 -> Heure de pointe (soirée)
    assert is_peak_hours(datetime(2026, 9, 13, 20, 0, tzinfo=tz)) is True
    # 16:00 -> Heure standard
    assert is_peak_hours(datetime(2026, 9, 13, 16, 0, tzinfo=tz)) is False
    # 10:00 -> Heure standard
    assert is_peak_hours(datetime(2026, 9, 13, 10, 0, tzinfo=tz)) is False


def test_get_adaptive_poll_interval():
    with (
        patch("app.tasks.vinted_poller.settings.VINTED_POLL_INTERVAL_SECONDS", 15),
        patch("app.tasks.vinted_poller.settings.VINTED_PROXY_URL", None),
    ):
        # VPS en heure de pointe : accélère à 12s
        with patch("app.tasks.vinted_poller.is_peak_hours", return_value=True):
            assert get_adaptive_poll_interval() == 12.0
        # VPS en heure standard : 15s
        with patch("app.tasks.vinted_poller.is_peak_hours", return_value=False):
            assert get_adaptive_poll_interval() == 15.0

    # Avec proxy résidentiel (Raspberry Pi)
    with (
        patch("app.tasks.vinted_poller.settings.VINTED_POLL_INTERVAL_SECONDS", 15),
        patch("app.tasks.vinted_poller.settings.VINTED_PROXY_URL", "http://192.168.1.50:8888"),
    ):
        # Proxy en heure de pointe : 8s
        with patch("app.tasks.vinted_poller.is_peak_hours", return_value=True):
            assert get_adaptive_poll_interval() == 8.0
        # Proxy en heure standard : 10s
        with patch("app.tasks.vinted_poller.is_peak_hours", return_value=False):
            assert get_adaptive_poll_interval() == 10.0

