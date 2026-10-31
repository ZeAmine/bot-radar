import sys
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from aiogram.methods import SendMessage

from app.bot.bot_instance import create_bot
from app.main import create_app
from scripts import chasse_prices


async def test_legacy_state_does_not_close_http_entrypoints(tmp_path, monkeypatch):
    state = tmp_path / 'state.json'
    state.write_text('{"mode":"pilot","halted":true}')
    monkeypatch.setenv('CHASSE_PILOT_STATE_PATH', str(state))
    app = create_app()
    assert not any(m.cls.__name__ == 'PilotMaintenanceMiddleware' for m in app.user_middleware)
    app.router.routes.clear()
    for path in ['/api/v1/ingest/listings', '/api/v1/telegram/webhook/example']:
        app.add_api_route(path, lambda: {'ok': True}, methods=['POST'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://test') as client:
        for path in ['/api/v1/ingest/listings', '/api/v1/telegram/webhook/example']:
            assert (await client.post(path)).status_code == 200


async def test_telegram_transport_has_no_legacy_guard(tmp_path, monkeypatch):
    state = tmp_path / 'state.json'
    state.write_text('{"mode":"pilot","halted":true}')
    monkeypatch.setenv('CHASSE_PILOT_STATE_PATH', str(state))
    bot = create_bot()
    request = AsyncMock(return_value='delivered')
    monkeypatch.setattr(bot.session, 'make_request', request)
    try:
        assert await bot(SendMessage(chat_id=1, text='synthetic')) == 'delivered'
        request.assert_awaited_once()
    finally:
        await bot.session.close()


def test_history_probe_requires_explicit_source_access(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['chasse_prices', 'probe', '--isbn', '9782756079332'])
    execute = Mock()
    monkeypatch.setattr(chasse_prices.asyncio, 'run', execute)
    with pytest.raises(SystemExit) as error:
        chasse_prices.main()
    assert error.value.code == 2
    execute.assert_not_called()


async def test_legacy_state_does_not_neutralize_scheduled_operations(tmp_path, monkeypatch):
    import importlib

    module = importlib.import_module('app.tasks.scheduler')
    state = tmp_path / 'state.json'
    state.write_text('{"mode":"pilot","halted":true}')
    monkeypatch.setenv('CHASSE_PILOT_STATE_PATH', str(state))
    recover = AsyncMock()
    refresh = AsyncMock(return_value={'processed': 1})
    monkeypatch.setattr(module, 'recover_notifications', recover)
    monkeypatch.setattr(module, 'refresh_book_prices_task', refresh)
    scheduler = module.create_scheduler()
    await scheduler.get_job('recover_notifications').func()
    await scheduler.get_job('refresh_prices').func()
    recover.assert_awaited_once()
    refresh.assert_awaited_once()
    # Une seule passe : aucun mode à transmettre, et aucun lot imposé d'avance —
    # le nombre de livres est la capacité libre lue au régulateur.
    assert 'incremental' not in refresh.call_args.kwargs
    assert refresh.call_args.kwargs.get('batch_size') is None
    for job in scheduler.get_jobs():
        assert job.max_instances == 1
        assert job.coalesce is True
