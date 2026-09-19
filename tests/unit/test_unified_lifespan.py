"""Tests unitaires pour le cycle de vie unifié de l'application FastAPI."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from app.main import lifespan


@pytest.mark.asyncio
async def test_unified_lifespan_starts_and_stops_services():
    """Vérifie que le lifespan FastAPI démarre et arrête proprement APScheduler et le poller."""
    mock_scheduler = MagicMock()
    mock_bot = MagicMock()
    mock_poller_task = AsyncMock()

    test_app = FastAPI()

    with (
        patch("app.main.setup_logging"),
        patch("app.bot.bot_instance.bot", mock_bot),
        patch("app.bot.bot_instance.setup_bot_commands", new_callable=AsyncMock) as mock_setup_cmds,
        patch("app.tasks.scheduler.scheduler", mock_scheduler),
        patch("app.tasks.vinted_poller.run_vinted_poller", mock_poller_task),
        patch("app.core.tasks.safe_create_task") as mock_create_task,
        patch("app.core.http_client.close_shared_http_client", new_callable=AsyncMock),
        patch("app.core.redis.close_redis", new_callable=AsyncMock),
        patch("app.core.tasks.wait_all_background_tasks", new_callable=AsyncMock),
        patch("app.core.config.settings.VINTED_POLLER_ENABLED", True),
    ):
        mock_task = AsyncMock()
        mock_task.cancel = MagicMock()
        mock_create_task.return_value = mock_task

        async with lifespan(test_app):
            mock_setup_cmds.assert_called_once_with(mock_bot)
            mock_scheduler.start.assert_called_once()
            mock_create_task.assert_called_once()

        mock_scheduler.shutdown.assert_called_once_with(wait=False)
        mock_task.cancel.assert_called_once()
