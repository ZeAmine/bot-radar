import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.providers.chasse_session import ChasseSessionManager


class MemoryStore:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **_kwargs):
        self.values[key] = value
        return True


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setattr(settings, "CHASSE_AUX_LIVRES_DUIH", "expired")
    monkeypatch.setattr(settings, "CHASSE_ACCOUNT_EMAIL", SecretStr("member@example.invalid"))
    monkeypatch.setattr(settings, "CHASSE_ACCOUNT_PASSWORD", SecretStr("private-password"))


def transport_handler(calls, *, accepts_login=True):
    def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/rest/jcd":
            if request.url.params.get("duih") == "renewed.signature":
                return httpx.Response(200, json={"stats": []})
            return httpx.Response(200, text="")
        if request.url.path == "/login" and request.method == "GET":
            return httpx.Response(
                200,
                text=('<form id="login"><input name="_sourcePage" value="page">'
                      '<input name="__fp" value="fp"></form>'),
            )
        if request.url.path == "/login":
            return httpx.Response(302, headers={"location": "/user/memo"})
        if request.url.path == "/":
            return httpx.Response(
                200,
                text='<html data-duih="renewed.signature">'
                if accepts_login
                else '<form id="login"></form>',
            )
        raise AssertionError(request.url.path)

    return respond


async def history(manager, client):
    return await manager.get_history(
        client,
        "https://www.chasse-aux-livres.fr/prix/test",
        {"id": "test", "didh": "test", "days": "365", "all": "true"},
        {},
    )


@pytest.mark.usefixtures("credentials")
async def test_expired_configured_token_is_renewed_and_survives_manager_restart():
    calls = []
    store = MemoryStore()
    notify = AsyncMock(return_value=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport_handler(calls))) as client:
        first = ChasseSessionManager(store=store, notify=notify)
        assert (await history(first, client)).json() == {"stats": []}
        second = ChasseSessionManager(store=store, notify=notify)
        assert (await history(second, client)).json() == {"stats": []}
    assert calls.count(("POST", "/login")) == 1
    notify.assert_not_awaited()


@pytest.mark.usefixtures("credentials")
async def test_concurrent_history_failures_share_one_reconnection():
    calls = []
    manager = ChasseSessionManager(store=MemoryStore(), notify=AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport_handler(calls))) as client:
        responses = await asyncio.gather(*(history(manager, client) for _ in range(5)))
    assert all(response.json() == {"stats": []} for response in responses)
    assert calls.count(("POST", "/login")) == 1


@pytest.mark.usefixtures("credentials")
async def test_http_200_without_member_session_is_not_a_success_and_notifies_once():
    calls = []
    notify = AsyncMock(return_value=True)
    manager = ChasseSessionManager(store=MemoryStore(), notify=notify)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler(calls, accepts_login=False))
    ) as client:
        for _ in range(3):
            response = await history(manager, client)
            assert response.content == b""
    assert calls.count(("POST", "/login")) == 1
    notify.assert_awaited_once()
    assert "private-password" not in str(notify.call_args)


@pytest.mark.usefixtures("credentials")
async def test_explicit_config_change_does_not_reuse_old_persisted_session(monkeypatch):
    calls = []
    store = MemoryStore()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport_handler(calls))) as client:
        manager = ChasseSessionManager(store=store, notify=AsyncMock())
        await history(manager, client)
        monkeypatch.setattr(settings, "CHASSE_AUX_LIVRES_DUIH", "new-bootstrap")
        manager = ChasseSessionManager(store=store, notify=AsyncMock())
        await history(manager, client)
    assert calls.count(("POST", "/login")) == 2
    assert all(isinstance(json.loads(value), dict) for value in store.values.values())


@pytest.mark.usefixtures('credentials')
async def test_access_recovers_after_rest_and_can_notify_a_later_incident():
    from datetime import UTC, datetime, timedelta

    store = MemoryStore()
    notify = AsyncMock(return_value=True)
    manager = ChasseSessionManager(store=store, notify=notify)
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler(calls, accepts_login=False))
    ) as client:
        await history(manager, client)
    manager.state['retry_at'] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler(calls))
    ) as client:
        assert (await history(manager, client)).json() == {'stats': []}
    assert 'retry_at' not in manager.state
    assert 'notified' not in manager.state
    assert calls.count(('POST', '/login')) == 2
    notify.assert_awaited_once()


@pytest.mark.usefixtures('credentials')
async def test_server_failure_does_not_reconnect_or_request_account_intervention():
    notify = AsyncMock()
    manager = ChasseSessionManager(store=MemoryStore(), notify=notify)
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert (await history(manager, client)).status_code == 503
    assert calls == ['/rest/jcd']
    notify.assert_not_awaited()


@pytest.mark.usefixtures('credentials')
async def test_schema_change_is_not_reported_as_an_expired_account():
    notify = AsyncMock()
    calls = []
    manager = ChasseSessionManager(store=MemoryStore(), notify=notify)

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={'changed_schema': True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await history(manager, client)
    assert response.json() == {'changed_schema': True}
    assert calls == ['/rest/jcd']
    notify.assert_not_awaited()


@pytest.mark.usefixtures('credentials')
async def test_maintenance_checks_the_session_without_collecting_book_offers(monkeypatch):
    from datetime import UTC, datetime, timedelta

    calls = []
    store = MemoryStore()
    manager = ChasseSessionManager(store=store, notify=AsyncMock())
    await manager.token()
    manager.state.update(
        probe_url='https://www.chasse-aux-livres.fr/prix/test',
        verified_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
    )
    fallback = transport_handler(calls)

    def respond(request):
        if request.url.path == '/prix/test':
            calls.append((request.method, request.url.path))
            return httpx.Response(200, text='<div data-asin="test" data-didh="test"></div>')
        return fallback(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr('app.core.http_client.get_shared_http_client', lambda: client)
        await manager.maintain()
        before = list(calls)
        await manager.maintain()
    assert calls == before
    assert calls.count(('POST', '/login')) == 1
    assert all('/lookup/' not in path for _, path in calls)


@pytest.mark.usefixtures('credentials')
async def test_a_captcha_stops_before_sending_account_credentials():
    calls = []
    notify = AsyncMock(return_value=True)
    manager = ChasseSessionManager(store=MemoryStore(), notify=notify)

    def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/rest/jcd':
            return httpx.Response(200, text='')
        return httpx.Response(200, text=(
            '<div class="g-recaptcha"></div><form id="login">'
            '<input name="_sourcePage" value="page"><input name="__fp" value="fp"></form>'
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert (await history(manager, client)).content == b''
    assert ('POST', '/login') not in calls
    notify.assert_awaited_once()


@pytest.mark.usefixtures('credentials')
async def test_incident_rest_and_notification_survive_manager_restart():
    store = MemoryStore()
    calls = []
    notify = AsyncMock(return_value=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler(calls, accepts_login=False))
    ) as client:
        first = ChasseSessionManager(store=store, notify=notify)
        await history(first, client)
        second = ChasseSessionManager(store=store, notify=notify)
        response = await history(second, client)
    assert response.status_code == 401
    assert response.extensions['request_skipped'] is True
    assert calls.count(('POST', '/login')) == 1
    notify.assert_awaited_once()


@pytest.mark.usefixtures('credentials')
async def test_a_valid_bootstrap_token_does_not_reconnect(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, 'CHASSE_AUX_LIVRES_DUIH', 'renewed.signature')
    manager = ChasseSessionManager(store=MemoryStore(), notify=AsyncMock())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler(calls))
    ) as client:
        assert (await history(manager, client)).json() == {'stats': []}
    assert calls == [('GET', '/rest/jcd')]
    assert manager.login_attempted is False


@pytest.mark.usefixtures('credentials')
async def test_reconnection_error_does_not_log_secret_values(monkeypatch, caplog):
    calls = []
    manager = ChasseSessionManager(store=MemoryStore(), notify=AsyncMock(return_value=True))

    async def unavailable(*_args):
        raise httpx.ConnectError('private-password')

    monkeypatch.setattr(manager, '_login', unavailable)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler(calls))
    ) as client:
        await history(manager, client)
    assert 'private-password' not in caplog.text
    assert 'ConnectError' in caplog.text


@pytest.mark.usefixtures('credentials')
async def test_history_robot_page_does_not_trigger_login_or_account_notification():
    calls = []
    notify = AsyncMock()
    store = MemoryStore()
    manager = ChasseSessionManager(store=store, notify=notify)

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, text='<title>Vérification robot</title>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await history(manager, client)
    assert calls == ['/rest/jcd']
    assert response.status_code == 200
    assert not response.extensions.get('chasse_auth_failed')
    assert not manager.login_attempted
    assert 'retry_at' not in manager.state
    notify.assert_not_awaited()
