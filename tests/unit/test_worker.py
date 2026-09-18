"""Tests unitaires pour le worker autonome APScheduler."""

import asyncio
import contextlib
from unittest.mock import MagicMock, patch

import pytest

from app.worker import main, run_worker


@pytest.mark.asyncio
async def test_run_worker_startup_and_shutdown():
    """Vérifie le démarrage et l'arrêt propre du planificateur dans le worker."""
    mock_scheduler = MagicMock()

    async def dummy_poller(stop_event):
        with contextlib.suppress(asyncio.CancelledError):
            await stop_event.wait()

    with (
        patch("app.worker.scheduler", mock_scheduler),
        patch("app.worker.run_vinted_poller", side_effect=dummy_poller),
    ):
        # Démarrage du worker dans une tâche asyncio
        task = asyncio.create_task(run_worker())
        await asyncio.sleep(0.02)

        mock_scheduler.start.assert_called_once()

        # Arrêt de la tâche pour déclencher le bloc finally
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        mock_scheduler.shutdown.assert_called_once_with(wait=False)


def test_worker_main_keyboard_interrupt():
    """Vérifie que main intercepte KeyboardInterrupt sans lever d'exception."""
    with (
        patch("app.worker.run_worker", MagicMock(return_value=None)),
        patch("app.worker.asyncio.run", side_effect=KeyboardInterrupt),
    ):
        # Doit se terminer sans lever d'exception
        main()


def test_settings_get_vinted_proxy():
    """Vérifie la résolution de proxy unique ou multiple."""
    from app.core.config import Settings

    # Aucun proxy
    s1 = Settings(VINTED_PROXY_URL=None)
    assert s1.get_vinted_proxy() is None

    # Proxy unique
    s2 = Settings(VINTED_PROXY_URL="http://user:pass@1.2.3.4:8080")
    assert s2.get_vinted_proxy() == "http://user:pass@1.2.3.4:8080"

    # Pool de proxys séparés par virgules
    s3 = Settings(VINTED_PROXY_URL="http://proxy1:80, http://proxy2:80")
    assert s3.get_vinted_proxy() in ["http://proxy1:80", "http://proxy2:80"]


@pytest.mark.asyncio
async def test_poll_vinted_once_separates_vinted_and_api_clients():
    """Vérifie que poll_vinted_once utilise vinted_client pour Vinted et api_client pour l'ingestion."""
    from unittest.mock import AsyncMock
    from app.services.vinted_scraper import ScrapedVintedItem
    from app.tasks.vinted_poller import poll_vinted_once

    mock_vinted_client = AsyncMock()
    mock_api_client = AsyncMock()

    # Simuler une annonce découverte
    dummy_item = ScrapedVintedItem(
        external_id="v_999",
        url="https://vinted.fr/items/999",
        title="Tintin au Tibet",
        condition="Très bon état",
        item_price_cents=1000,
        photo_url="https://images1.vinted.net/t.jpg",
    )

    mock_post_resp = MagicMock()
    mock_post_resp.status_code = 200
    mock_post_resp.json.return_value = {"matches": []}
    mock_api_client.post.return_value = mock_post_resp

    seen_ids = set()

    with patch("app.tasks.vinted_poller.fetch_vinted_page", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = [dummy_item]

        count = await poll_vinted_once(
            seen_ids=seen_ids,
            vinted_client=mock_vinted_client,
            api_client=mock_api_client,
            api_url="http://app:8000",
            api_token="test_token",
        )

        assert count == 1
        assert "v_999" in seen_ids
        # fetch_vinted_page doit avoir reçu vinted_client
        mock_fetch.assert_called_once_with(
            "https://www.vinted.fr/catalog?search_text=&catalog[]=5425&status_ids[]=1&status_ids[]=2&status_ids[]=6&language_book_ids[]=6436&search_id=2946920741&order=newest_first",
            mock_vinted_client,
        )
        # L'ingestion API interne doit avoir été postée via api_client (sans proxy)
        mock_api_client.post.assert_called_once()
        args, kwargs = mock_api_client.post.call_args
        assert args[0] == "http://app:8000/api/v1/ingest/listings"
        mock_vinted_client.post.assert_not_called()
