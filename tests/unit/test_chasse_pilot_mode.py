import importlib
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.market_backfill import manifest_digest


def manifest():
    codes = ['9782756079332', '9782205049657', '9782205051995', '9782205059250',
             '9782808500487', '9782413091424', '9791026856405']
    value = {'version': 1, 'scope': 'pilot_only', 'collection_isbn13': codes,
             'maximum_unique_collections': 7, 'books': [], 'alerts': [],
             'catalogue_repairs': [], 'isbn_aliases': []}
    value['digest'] = manifest_digest(value)
    return value


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    from app.core import pilot_mode

    path = tmp_path / 'state.json'
    monkeypatch.setattr(settings, 'CHASSE_PILOT_STATE_PATH', str(path))
    value = manifest()
    pilot_mode.enable(path, value, value['digest'])
    return pilot_mode, path, value


def test_pilot_survives_module_restart_and_never_expires(pilot):
    mode, path, value = pilot
    assert mode.active()
    with mode.collection_scope('9782756079332'):
        mode.finish_collection('complete', [])
    restarted = importlib.reload(mode)
    assert restarted.active()
    assert restarted.was_attempted('9782756079332')
    assert json.loads(path.read_text())['mode'] == 'pilot'
    assert restarted.read_state()['manifest']['digest'] == value['digest']
    with pytest.raises(mode.PilotBlockedError), mode.collection_scope('9782756079332'):
        pytest.fail('Une seconde collecte a été admise')


def test_pilot_refuses_out_of_scope_and_halts_remaining_work(pilot):
    mode, _, _ = pilot
    with pytest.raises(mode.PilotBlockedError), mode.collection_scope('9782413044932'):
        pytest.fail('Édition hors périmètre admise')
    assert mode.read_state()['halted']
    with pytest.raises(mode.PilotBlockedError), mode.collection_scope('9782756079332'):
        pytest.fail('Le pilote arrêté a repris')


@pytest.mark.parametrize('reason', ['rate_limited', 'bot_challenge', 'session_unavailable'])
def test_source_access_failure_stops_immediately(pilot, reason):
    mode, _, _ = pilot
    with mode.collection_scope('9782756079332'):
        mode.finish_collection('failed', [reason])
    assert mode.read_state()['halted']


def test_two_identical_failures_stop_and_state_is_persistent(pilot):
    mode, _, _ = pilot
    for code in ('9782756079332', '9782205049657'):
        with mode.collection_scope(code):
            mode.finish_collection('partial', ['timeout'])
    assert mode.read_state()['halted']
    assert mode.read_state()['consecutive_failures'] == 2


def test_missing_or_corrupt_configured_state_fails_closed(pilot):
    mode, path, _ = pilot
    path.write_text('invalid')
    assert mode.active()
    with pytest.raises(mode.PilotBlockedError):
        mode.require_source('9782756079332')
    path.unlink()
    assert mode.active()


def test_release_requires_explicit_flag_and_matching_digest(pilot):
    mode, path, value = pilot
    with pytest.raises(mode.PilotBlockedError):
        mode.release(path, value['digest'], authorize=False)
    assert mode.active()
    mode.release(path, value['digest'], authorize=True)
    assert not mode.active()
    assert mode.read_state()['manifest']['digest'] == value['digest']


async def test_scheduled_jobs_do_nothing_in_pilot(pilot):
    from app.tasks.scheduler import create_scheduler

    scheduler = create_scheduler()
    for job in scheduler.get_jobs():
        await job.func()
    assert pilot[0].read_state()['attempts'] == {}


async def test_telegram_messages_and_admin_notifications_are_blocked(pilot):
    from types import SimpleNamespace

    from app.services.telegram_service import notify_admins, send_listing_alert

    assert pilot[0].active()
    bot = AsyncMock()
    listing = SimpleNamespace(id='test')
    result = await send_listing_alert(bot, 42, listing)
    assert result.outcome == 'not_sent'
    assert not await notify_admins('test', bot=bot)
    assert bot.mock_calls == []


def test_pilot_blocks_ingestion_and_telegram_before_any_database_mutation(pilot):
    from app.main import create_app

    client = TestClient(create_app())
    for route in ('/api/v1/ingest/listings', '/api/v1/telegram/webhook/example',
                  '/api/v1/books'):
        response = client.post(route, json={})
        assert response.status_code == 503
    assert pilot[0].read_state()['attempts'] == {}


async def test_bot_transport_itself_refuses_messages_in_pilot(pilot):
    from aiogram.methods import SendMessage

    from app.bot.bot_instance import create_bot

    instance = create_bot()
    try:
        middleware = tuple(instance.session.middleware)[0]
        transport = AsyncMock()
        with pytest.raises(pilot[0].PilotBlockedError):
            await middleware(transport, instance, SendMessage(chat_id=42, text='test'))
        transport.assert_not_called()
    finally:
        await instance.session.close()


def test_enable_same_manifest_keeps_attempts_and_never_resets_the_budget(pilot):
    mode, path, value = pilot
    with mode.collection_scope('9782756079332'):
        mode.finish_collection('complete', [])
    mode.enable(path, value, value['digest'])
    assert mode.was_attempted('9782756079332')
    assert len(mode.read_state()['attempts']) == 1


def test_a_separate_process_finds_the_persistent_pilot_budget(pilot, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    mode, path, _ = pilot
    with mode.collection_scope('9782756079332'):
        mode.finish_collection('complete', [])
    root = Path(__file__).resolve().parents[2]
    program = '''
import socket
from pydantic_settings import BaseSettings
original = BaseSettings.__init__
def init(self, *args, **kwargs):
    kwargs.update(_env_file=None, _secrets_dir=None)
    original(self, *args, **kwargs)
BaseSettings.__init__ = init
BaseSettings.settings_customise_sources = classmethod(
    lambda cls, settings_cls, **values: (values['init_settings'], values['env_settings']))
def deny(*args, **kwargs):
    raise PermissionError('Transport interdit dans ce processus')
socket.socket.connect = deny
from app.core import pilot_mode
assert pilot_mode.active()
assert pilot_mode.was_attempted('9782756079332')
assert len(pilot_mode.read_state()['attempts']) == 1
print('persistent_pilot_verified')
'''
    result = subprocess.run([sys.executable, '-B', '-c', program], cwd=tmp_path,
                            env={'PATH': os.defpath, 'PYTHONPATH': str(root), 'APP_ENV': 'test',
                                 'CHASSE_PILOT_STATE_PATH': str(path)},
                            capture_output=True, text=True, timeout=15, check=True)
    assert result.stdout.strip() == 'persistent_pilot_verified'


async def test_backfill_uses_embedded_approved_manifest_after_restart(pilot, monkeypatch):
    from argparse import Namespace

    from app.services import market_backfill
    from scripts.backfill_chasse_prices import run

    mode, path, value = pilot
    apply = AsyncMock(return_value={"pilot_halted": False})
    monkeypatch.setattr(market_backfill, "apply_market_backfill", apply)
    assert await run(Namespace(apply=True, pilot_state=path, manifest=None,
                               approved_digest=value["digest"], limit=7)) == 0
    apply.assert_awaited_once_with(value, digest=value["digest"], limit=7)
    assert mode.active()


def test_failed_session_probe_halts_the_persistent_pilot(pilot):
    mode, _, _ = pilot
    with (
        pytest.raises(ValueError, match='Sonde interrompue'),
        mode.session_probe_scope('9782756079332'),
    ):
        raise ValueError('Sonde interrompue')
    assert mode.read_state()['halted']
    assert mode.read_state()['session_probes'] == 1


async def test_pilot_blocks_direct_automatic_session_maintenance(pilot, monkeypatch):
    from app.providers.chasse_session import ChasseSessionManager

    assert pilot[0].active()
    manager = ChasseSessionManager()
    load = AsyncMock()
    monkeypatch.setattr(manager, '_load', load)
    await manager.maintain()
    load.assert_not_called()


def test_manual_recovery_preserves_attempts_and_allows_one_failed_retry(pilot):
    from datetime import UTC, datetime, timedelta

    mode, path, value = pilot
    code = "9782808500487"
    with mode.collection_scope(code):
        mode.finish_collection("failed", ["bot_challenge"])
    failed = mode.read_state()["attempts"][code]
    mode.resume(path, value["digest"], isbn=code, authorize=True,
                not_before=datetime.now(UTC) - timedelta(seconds=1))
    restarted = importlib.reload(mode)
    assert restarted.active()
    assert not restarted.read_state()["halted"]
    assert not restarted.was_attempted(code)
    assert restarted.read_state()["attempts"][code] == failed
    with restarted.collection_scope(code):
        restarted.finish_collection("complete", [])
    state = restarted.read_state()
    assert state["attempts"][code]["previous_attempts"] == [failed]
    assert state["attempts"][code]["status"] == "complete"
    assert state["manifest"] == value
    assert len(state["attempts"]) == 1
    assert restarted.was_attempted(code)
    with pytest.raises(restarted.PilotBlockedError), restarted.collection_scope(code):
        pytest.fail("Une troisième collecte a été admise")


def test_manual_recovery_refuses_early_retry_and_does_not_change_state(pilot):
    from datetime import UTC, datetime, timedelta

    mode, path, value = pilot
    with mode.collection_scope("9782808500487"):
        mode.finish_collection("failed", ["bot_challenge"])
    before = path.read_bytes()
    with pytest.raises(mode.PilotBlockedError):
        mode.resume(path, value["digest"], isbn="9782808500487", authorize=True,
                    not_before=datetime.now(UTC) + timedelta(hours=6))
    assert path.read_bytes() == before


@pytest.mark.parametrize("case", ["unauthorized", "digest", "scope", "successful",
                                  "interrupted", "second_recovery"])
def test_manual_recovery_rejects_unsafe_or_repeated_authorizations(pilot, case):
    from datetime import UTC, datetime, timedelta

    mode, path, value = pilot
    code = "9782808500487"
    if case == "interrupted":
        with pytest.raises(RuntimeError), mode.collection_scope(code):
            raise RuntimeError("Interrupted")
    else:
        with mode.collection_scope(code):
            mode.finish_collection("complete" if case == "successful" else "failed",
                                   [] if case == "successful" else ["bot_challenge"])
        if case == "successful":
            mode.halt("source_failure")
    past = datetime.now(UTC) - timedelta(seconds=1)
    if case == "second_recovery":
        mode.resume(path, value["digest"], isbn=code, authorize=True, not_before=past)
        with mode.collection_scope(code):
            mode.finish_collection("failed", ["bot_challenge"])
    before = path.read_bytes()
    with pytest.raises(mode.PilotBlockedError):
        mode.resume(path, "different" if case == "digest" else value["digest"],
                    isbn="9782205055641" if case == "scope" else code,
                    authorize=case != "unauthorized", not_before=past)
    assert path.read_bytes() == before


@pytest.mark.parametrize("missing", ["--isbn", "--approved-digest", "--authorize-pilot-retry"])
def test_resume_command_requires_explicit_scope_and_authorization(pilot, monkeypatch, missing):
    from scripts.chasse_pilot import main

    mode, path, value = pilot
    args = ["chasse_pilot.py", "resume", "--state", str(path)]
    for flag, val in (("--isbn", "9782808500487"), ("--approved-digest", value["digest"]),
                      ("--authorize-pilot-retry", None)):
        if flag != missing:
            args.append(flag)
            if val is not None:
                args.append(val)
    monkeypatch.setattr("sys.argv", args)
    before = path.read_bytes()
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert path.read_bytes() == before
    assert mode.active()
