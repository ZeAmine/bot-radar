import asyncio
import sys
from datetime import UTC, datetime, timedelta
from functools import partial

import httpx
import pytest

from app.providers.cal_governor import CalCollectionBudget, CalGovernor, CalSuspendedError
from app.providers.chasse_session import ChasseSessionManager
from scripts import refresh_chasse_token as refresh
from tests.unit.test_chasse_aux_livres_provider import (
    MOCK_LOOKUP_JSON,
    MOCK_PROD_HTML,
    MOCK_REST_JSON,
    MOCK_SEARCH_PAGE_HTML,
)
from tests.unit.test_chasse_session import MemoryStore
from tests.unit.test_refresh_chasse_token import LOGIN_PAGE_HTML, MEMBER_HOME_HTML

pytest_plugins = ["tests.unit.cal_redis_fixture"]


@pytest.mark.parametrize("restriction", ["open", "foreign_probe", "unavailable"])
async def test_refresh_refuses_before_any_http(
    restriction, clean_cal_keys, cal_namespace, monkeypatch,
):
    now = [1_800_000_000.0]
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace, clock=lambda: now[0])
    if restriction != "unavailable":
        await governor.suspend("bot_challenge", 10)
    if restriction == "foreign_probe":
        now[0] += 11
        await governor.admit_book("another-collection")
    if restriction == "unavailable":
        governor.store = False
    seen = []

    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(503)

    transport = httpx.MockTransport(handle)
    async with httpx.AsyncClient(transport=transport) as client:
        monkeypatch.setattr(refresh, "cal_governor", governor, raising=False)
        monkeypatch.setattr(refresh, "get_shared_http_client", lambda: client, raising=False)
        monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=transport))
        monkeypatch.setenv("CHASSE_ACCOUNT_EMAIL", "synthetic@example.invalid")
        monkeypatch.setenv("CHASSE_ACCOUNT_PASSWORD", "synthetic")
        monkeypatch.setattr(sys, "argv", ["refresh_chasse_token.py", "--print-only"])
        assert await refresh.main() != 0
    assert seen == []


@pytest.fixture
def run_refresh(monkeypatch):
    async def run(governor, handler):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True,
        ) as client:
            monkeypatch.setattr(refresh, "cal_governor", governor)
            monkeypatch.setattr(refresh, "get_shared_http_client", lambda: client)
            monkeypatch.setattr(
                "app.providers.chasse_aux_livres.get_shared_http_client", lambda: client,
            )
            monkeypatch.setattr(
                "app.providers.chasse_aux_livres.chasse_session",
                ChasseSessionManager(store=MemoryStore()),
            )
            monkeypatch.setitem(refresh.validate_token.__globals__, "PROBE_CANDIDATES", (
                ("9791095670568", "Rip Tome 1"),
            ))
            monkeypatch.setenv("CHASSE_ACCOUNT_EMAIL", "synthetic@example.invalid")
            monkeypatch.setenv("CHASSE_ACCOUNT_PASSWORD", "synthetic")
            monkeypatch.setattr(sys, "argv", ["refresh_chasse_token.py", "--print-only"])
            return await refresh.main()
    return run


def _successful_response(request):
    path = request.url.path
    if path == "/login":
        if request.method == "GET":
            return httpx.Response(200, text=LOGIN_PAGE_HTML)
        return httpx.Response(302, headers={"location": "/user/memo"})
    if path == "/user/memo":
        return httpx.Response(200, text="membre")
    if path == "/":
        return httpx.Response(200, text=MEMBER_HOME_HTML)
    if path == "/search":
        return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
    if path == "/rest/search-results":
        return httpx.Response(200, json=MOCK_REST_JSON)
    if path.startswith("/prix/"):
        return httpx.Response(200, text=MOCK_PROD_HTML.replace(
            "></div>", ' data-didh="abcd"></div>',
        ))
    if path == "/rest/jcd":
        return httpx.Response(200, json={"stats": [{
            "label": "Très bon état +", "statValues": [{
                "x": (datetime.now(UTC) - timedelta(days=1)).isoformat(), "y": 9.21,
            }],
        }]})
    if path == "/rest/lookup/results":
        return httpx.Response(200, json=MOCK_LOOKUP_JSON)
    raise AssertionError(path)


async def test_closed_refresh_validates_with_one_budget_and_no_book_admission(
    clean_cal_keys, cal_namespace, run_refresh,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace)
    seen = []

    async def handle(request):
        seen.append(request.url.path)
        return _successful_response(request)

    assert await run_refresh(governor, handle) == 0
    assert seen[:4] == ["/login", "/login", "/user/memo", "/"]
    assert seen[-1] == "/rest/jcd"
    assert governor.emitted == len(seen)
    assert await clean_cal_keys.zcard(governor.admit_key) == 0
    assert await clean_cal_keys.zcard(governor.window_key) == len(seen)


@pytest.mark.parametrize("interruption", ["open", "challenge_redirect", "unavailable", "external"])
async def test_refresh_rechecks_each_request(
    interruption, clean_cal_keys, cal_namespace, run_refresh,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace,
                           clock=lambda: 1_800_000_000.0)
    seen = []

    async def handle(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/must-not-follow":
            return httpx.Response(200, text="")
        if interruption == "open":
            await governor.suspend("bot_challenge", 3600)
        elif interruption == "unavailable":
            governor.store = False
        elif interruption == "challenge_redirect":
            return httpx.Response(302, headers={
                "cf-mitigated": "challenge", "location": "/must-not-follow",
            })
        elif interruption == "external":
            return httpx.Response(302, headers={"location": "https://example.invalid/"})
        return _successful_response(request)

    assert await run_refresh(governor, handle) != 0
    assert seen == [("GET", "/login")]
    if interruption == "challenge_redirect":
        snapshot = await governor.snapshot()
        assert snapshot.reason == "bot_challenge"
        assert snapshot.open_until == 1_800_021_600.0


@pytest.mark.parametrize("constraint", ["concurrency", "window"])
async def test_refresh_cannot_emit_while_request_budget_is_occupied(
    constraint, clean_cal_keys, cal_namespace, run_refresh,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace, max_wait_seconds=0)
    if constraint == "concurrency":
        await governor._acquire_slot(CalCollectionBudget(governor))
    else:
        now = governor.wall_now()
        await clean_cal_keys.zadd(governor.window_key, {
            f"occupied-{i}": now for i in range(governor.max_requests_per_window)
        })
    seen = []

    def handle(request):
        seen.append(request.url.path)
        return _successful_response(request)

    assert await run_refresh(governor, handle) != 0
    assert seen == []


async def test_probe_refresh_stays_exclusive_until_history_validation(
    clean_cal_keys, cal_namespace, run_refresh,
):
    now = [1_800_000_000.0]
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace, clock=lambda: now[0])
    contender = CalGovernor(store=clean_cal_keys, namespace=cal_namespace, clock=lambda: now[0])
    await governor.suspend("bot_challenge", 1)
    now[0] += 2
    denied = []

    async def handle(request):
        with pytest.raises(CalSuspendedError) as error:
            await contender.admit_book("contender")
        denied.append(error.value.reason)
        return _successful_response(request)

    assert await run_refresh(governor, handle) == 0
    assert denied
    assert set(denied) == {"probe_in_flight"}
    assert (await governor.snapshot()).open_until == 0
    assert await contender.admit_book("after-success") is None


async def test_cancelled_refresh_never_concludes_a_probe_successfully(
    clean_cal_keys, cal_namespace, run_refresh,
):
    now = [1_800_000_000.0]
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace, clock=lambda: now[0])
    await governor.suspend("bot_challenge", 1)
    now[0] += 2
    entered = asyncio.Event()

    async def handle(_request):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(run_refresh(governor, handle))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    snapshot = await governor.snapshot()
    assert snapshot.open_until > now[0]
    assert not snapshot.probe_pass
    assert await clean_cal_keys.zcard(governor.lease_key) == 0


async def test_validation_does_not_follow_redirects_outside_the_budget(
    clean_cal_keys, cal_namespace, run_refresh,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace)
    seen = []

    def handle(request):
        seen.append(request.url.path)
        if request.url.path == "/search":
            return httpx.Response(302, headers={"location": "/unregulated"})
        if request.url.path == "/unregulated":
            return httpx.Response(200, text="")
        return _successful_response(request)

    assert await run_refresh(governor, handle) != 0
    assert "/unregulated" not in seen
    assert len(seen) == governor.emitted


async def test_capture_and_validation_share_the_collection_request_ceiling(
    clean_cal_keys, cal_namespace, run_refresh,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace,
                           max_requests_per_collection=4)
    seen = []

    def handle(request):
        seen.append(request.url.path)
        return _successful_response(request)

    assert await run_refresh(governor, handle) != 0
    assert seen == ["/login", "/login", "/user/memo", "/"]
    assert governor.emitted == 4


async def test_login_redirect_307_preserves_the_form_body_and_content_type(
    clean_cal_keys, cal_namespace, run_refresh,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace)
    submitted = []

    def handle(request):
        if request.method == "POST" and request.url.path == "/login":
            return httpx.Response(307, headers={"location": "/login-submit"})
        if request.url.path == "/login-submit":
            submitted.append((request.method, request.headers.get("content-type"), request.content))
            return httpx.Response(302, headers={"location": "/user/memo"})
        return _successful_response(request)

    assert await run_refresh(governor, handle) == 0
    method, content_type, body = submitted[0]
    assert method == "POST"
    assert content_type == "application/x-www-form-urlencoded"
    assert b"email=synthetic%40example.invalid" in body
    assert b"_sourcePage=SOURCE-PAGE-123" in body
