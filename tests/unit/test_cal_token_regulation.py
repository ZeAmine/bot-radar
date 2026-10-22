import asyncio
import sys
from datetime import UTC, datetime, timedelta
from functools import partial

import httpx
import pytest

from app.providers.base import requested_components
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
        assert await refresh.main() == 3
    assert seen == []


@pytest.fixture
def run_refresh(monkeypatch):
    async def run(governor, handler, *, print_only=True, probe_count=1, trace=None):
        if trace is not None:
            original_request = governor.request
            original_probe = refresh.validate_token.__globals__["_probe_series"]
            trace.update(refusals=[], probes=0, transport_calls=0)

            async def record_request(client, budget, url, **kwargs):
                try:
                    return await original_request(client, budget, url, **kwargs)
                except CalSuspendedError as exc:
                    query = httpx.URL(url).params.get("query", "")
                    trace["refusals"].append({
                        "reason": exc.reason, "phase": "validation",
                        "search": "ISBN" if query.isdigit() else "title",
                        "used": budget.used, "transport_calls": trace["transport_calls"],
                    })
                    raise

            async def record_probe(*args, **kwargs):
                trace["probes"] += 1
                return await original_probe(*args, **kwargs)

            async def record_transport(request):
                trace["transport_calls"] += 1
                response = handler(request)
                return await response if asyncio.iscoroutine(response) else response

            monkeypatch.setattr(governor, "request", record_request)
            monkeypatch.setitem(refresh.validate_token.__globals__, "_probe_series", record_probe)
            transport_handler = record_transport
        else:
            transport_handler = handler
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport_handler), follow_redirects=True,
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
            ) * probe_count)
            monkeypatch.setenv("CHASSE_ACCOUNT_EMAIL", "synthetic@example.invalid")
            monkeypatch.setenv("CHASSE_ACCOUNT_PASSWORD", "synthetic")
            monkeypatch.setenv("BOTRADAR_API_URL", "https://example.invalid")
            monkeypatch.setattr(sys, "argv", [
                "refresh_chasse_token.py", *(["--print-only"] if print_only else []),
            ])
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

    assert await run_refresh(governor, handle) == (2 if interruption == "external" else 3)
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

    assert await run_refresh(governor, handle) == 3
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
    trace = {}

    def handle(request):
        seen.append(request.url.path)
        return _successful_response(request)

    assert await run_refresh(governor, handle, probe_count=2, trace=trace) == 3
    assert seen == ["/login", "/login", "/user/memo", "/"]
    assert governor.emitted == 4
    assert trace["refusals"] == [{
        "reason": "collection_budget", "phase": "validation", "search": "ISBN",
        "used": 4, "transport_calls": 4,
    }], trace
    assert governor.denied == 1
    assert trace["probes"] == 1


@pytest.mark.parametrize("restriction", ["unavailable", "open"])
async def test_restriction_during_validation_stops_fallback_and_second_probe(
    restriction,
    clean_cal_keys, cal_namespace, run_refresh,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace)
    seen = []
    trace = {}

    async def handle(request):
        seen.append(request.url.path)
        if request.url.path == "/":
            if restriction == "unavailable":
                governor.store = False
            else:
                await governor.suspend("bot_challenge", 3600)
        return _successful_response(request)

    assert await run_refresh(governor, handle, probe_count=2, trace=trace) == 3
    assert seen == ["/login", "/login", "/user/memo", "/"]
    assert len(trace["refusals"]) == 1, trace
    assert trace["probes"] == 1


async def test_empty_first_history_allows_second_probe_with_available_budget(
    clean_cal_keys, cal_namespace, run_refresh,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace)
    trace = {}
    histories = []

    def handle(request):
        if request.url.path == "/rest/jcd":
            histories.append(request.url.path)
            if len(histories) == 1:
                return httpx.Response(200, json={"stats": []})
        return _successful_response(request)

    components = requested_components.set(frozenset({"history"}))
    try:
        assert await run_refresh(governor, handle, probe_count=2, trace=trace) == 0
    finally:
        requested_components.reset(components)
    assert trace["probes"] == 2
    assert len(histories) == 2
    assert trace["refusals"] == []
    assert governor.emitted == trace["transport_calls"]


async def test_first_probe_exhausting_budget_does_not_start_second_probe(
    clean_cal_keys, cal_namespace, run_refresh,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace,
                           max_requests_per_collection=9)
    trace = {}

    def handle(request):
        if request.url.path == "/rest/jcd":
            return httpx.Response(200, json={"stats": []})
        return _successful_response(request)

    assert await run_refresh(governor, handle, probe_count=2, trace=trace) == 3
    assert trace["probes"] == 1, trace
    assert trace["refusals"] == []
    assert trace["transport_calls"] == governor.emitted == 9


async def test_empty_isbn_results_preserve_title_fallback_without_restriction(
    clean_cal_keys, cal_namespace, run_refresh,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace)
    searches = []
    results = []

    def handle(request):
        if request.url.path == "/search":
            searches.append(request.url.params["query"])
        if request.url.path == "/rest/search-results":
            results.append(request.url.path)
            if len(results) == 1:
                return httpx.Response(200, json={"d": ""})
        return _successful_response(request)

    assert await run_refresh(governor, handle) == 0
    assert searches == ["9791095670568", "Rip Tome 1"]
    assert governor.denied == 0


async def test_refused_token_returns_one_without_injection(
    clean_cal_keys, cal_namespace, run_refresh,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace)

    def handle(request):
        if request.url.path == "/rest/jcd":
            return httpx.Response(200, json={"stats": []})
        return _successful_response(request)

    assert await run_refresh(governor, handle) == 1


async def test_failed_injection_returns_one(
    clean_cal_keys, cal_namespace, run_refresh, monkeypatch,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace)
    injected = []

    def refuse(api_url, _internal_token, _token):
        injected.append(api_url)
        return False

    monkeypatch.setattr(refresh, "push_token_to_radar", refuse)
    assert await run_refresh(governor, _successful_response, print_only=False) == 1
    assert injected == ["https://example.invalid"]


async def test_successful_injection_returns_zero(
    clean_cal_keys, cal_namespace, run_refresh, monkeypatch,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace)
    injected = []

    def accept(api_url, _internal_token, _token):
        injected.append(api_url)
        return True

    monkeypatch.setattr(refresh, "push_token_to_radar", accept)
    assert await run_refresh(governor, _successful_response, print_only=False) == 0
    assert injected == ["https://example.invalid"]


@pytest.mark.parametrize("missing", ["CHASSE_ACCOUNT_EMAIL", "CHASSE_ACCOUNT_PASSWORD"])
async def test_missing_credentials_return_two_without_http(missing, monkeypatch):
    monkeypatch.setenv("CHASSE_ACCOUNT_EMAIL", "synthetic@example.invalid")
    monkeypatch.setenv("CHASSE_ACCOUNT_PASSWORD", "synthetic")
    monkeypatch.setenv(missing, "")
    monkeypatch.setattr(sys, "argv", ["refresh_chasse_token.py", "--print-only"])
    assert await refresh.main() == 2


@pytest.mark.parametrize("failure", ["http", "form", "login", "token"])
async def test_impossible_capture_returns_two(
    failure, clean_cal_keys, cal_namespace, run_refresh,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace)

    def handle(request):
        if failure == "http":
            return httpx.Response(503)
        if failure == "form":
            return httpx.Response(200, text="<html></html>")
        if failure == "login" and request.method == "POST":
            return httpx.Response(200, text=LOGIN_PAGE_HTML)
        if failure == "token" and request.url.path == "/":
            return httpx.Response(200, text="<html></html>")
        return _successful_response(request)

    assert await run_refresh(governor, handle) == 2


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
