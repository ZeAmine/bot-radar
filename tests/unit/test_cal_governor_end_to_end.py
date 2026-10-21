"""Effets de bout en bout du régulateur CAL sur le parcours applicatif réel."""


import time
from unittest.mock import AsyncMock

import httpx
import pytest

from app.providers.cal_governor import (
    CAL_MIN_INTERVAL_SECONDS,
    OPEN,
    CalCollectionBudget,
    CalGovernor,
    CalSuspendedError,
    GovernedClient,
    pass_occurrence,
)
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
from app.providers.chasse_session import ChasseSessionManager
from tests.unit.test_chasse_aux_livres_provider import (
    MOCK_LOOKUP_JSON,
    MOCK_PROD_HTML,
    MOCK_REST_JSON,
    MOCK_SEARCH_PAGE_HTML,
)

pytest_plugins = ["tests.unit.cal_redis_fixture"]

BASE = "https://www.chasse-aux-livres.fr"
ISBN = "9782035951463"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _governor(clock: FakeClock, store, namespace: str, **kwargs) -> CalGovernor:
    async def _advance(seconds: float) -> None:
        clock.now += seconds

    options = {
        "store": store, "clock": clock, "namespace": namespace, "sleep": _advance,
    }
    options.update(kwargs)
    return CalGovernor(**options)


@pytest.fixture
def pass_token():
    token = pass_occurrence.set("passe-e2e")
    yield token
    pass_occurrence.reset(token)


@pytest.mark.usefixtures("pass_token")
@pytest.mark.asyncio
async def test_a_deferral_before_any_call_is_not_a_provider_failure(clean_cal_keys, cal_namespace):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"offers": {}})

    clock = FakeClock()
    governor = _governor(clock, clean_cal_keys, cal_namespace)
    await governor.suspend("bot_challenge", 3600)

    async with _client(handler) as client:
        provider = ChasseAuxLivresProvider(
            http_client=client, max_retries=0, governor=governor,
        )
        offers = await provider.get_offers(isbn13=ISBN, title="Titre")

    assert calls["n"] == 0
    assert offers.failed_providers == ()
    assert [a.status for a in offers.attempts] == ["skipped"]
    assert offers.attempts[0].reason == "bot_challenge"
    assert list(offers) == []


async def test_exhausted_collection_budget_keeps_received_offers_as_partial(
    clean_cal_keys, cal_namespace,
):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url).replace(BASE, "")
        seen.append(path)
        if path.startswith("/search?query="):
            return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
        if path.startswith("/rest/search-results"):
            return httpx.Response(200, json=MOCK_REST_JSON)
        if path.startswith("/prix/"):
            return httpx.Response(200, text=MOCK_PROD_HTML.replace(
                'data-nbeng="1"', 'data-nbeng="2"',
            ))
        return httpx.Response(200, json=MOCK_LOOKUP_JSON)

    clock = FakeClock()
    governor = _governor(clock, clean_cal_keys, cal_namespace, max_requests_per_collection=4)
    async with _client(handler) as client:
        provider = ChasseAuxLivresProvider(
            http_client=client, max_retries=0, governor=governor,
        )
        offers = await provider.get_offers(isbn13=None, title="Rip Tome 1")

    assert len(seen) == 4
    assert governor.emitted == 4
    assert list(offers)
    current = [a for a in offers.attempts if a.operation == "current"]
    assert current
    assert current[0].status == "partial"
    assert current[0].reason == "incomplete_offers"
    assert sum(1 for offer in offers) >= 1
    assert governor.denied >= 1


@pytest.mark.usefixtures("pass_token")
@pytest.mark.asyncio
async def test_a_challenge_during_a_poll_blocks_every_later_call(clean_cal_keys, cal_namespace):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        path = request.url.path
        if path == "/search":
            return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
        if path == "/rest/search-results":
            return httpx.Response(200, json=MOCK_REST_JSON)
        if path.startswith("/prix/"):
            return httpx.Response(200, text=MOCK_PROD_HTML)
        if path == "/rest/lookup/results" and "retry=" in str(request.url):
            return httpx.Response(200, text="vérification robot")
        return httpx.Response(200, json=MOCK_LOOKUP_JSON)

    clock = FakeClock()
    governor = _governor(clock, clean_cal_keys, cal_namespace)
    async with _client(handler) as client:
        provider = ChasseAuxLivresProvider(
            http_client=client, max_retries=0, governor=governor,
        )
        await provider.get_offers(isbn13=ISBN, title="Rip Tome 1")
        emitted = calls["n"]
        assert governor.phase() == OPEN
        await provider.get_offers(isbn13="9782035951462", title="Rip Tome 1")

    assert calls["n"] == emitted


@pytest.mark.asyncio
async def test_maintenance_respects_an_open_circuit(clean_cal_keys, cal_namespace):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="<html></html>")

    clock = FakeClock()
    governor = _governor(clock, clean_cal_keys, cal_namespace)
    await governor.suspend("bot_challenge", 3600)

    async def no_notification(_message: str) -> bool:
        return False

    session = ChasseSessionManager(
        store=clean_cal_keys, notify=no_notification, governor=governor,
    )
    session.state = {"probe_url": BASE + "/prix/123/titre", "seed": session._seed()}
    session.loaded = True

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "app.core.http_client.get_shared_http_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        await session.maintain()

    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_maintenance_is_admitted_once_the_circuit_is_closed(clean_cal_keys, cal_namespace):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="<html></html>")

    governor = _governor(FakeClock(), clean_cal_keys, cal_namespace)

    async def no_notification(_message: str) -> bool:
        return False

    session = ChasseSessionManager(
        store=clean_cal_keys, notify=no_notification, governor=governor,
    )
    session.state = {"probe_url": BASE + "/prix/123/titre", "seed": session._seed()}
    session.loaded = True

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("app.core.http_client.get_shared_http_client", lambda: client)
        try:
            await session.maintain()
        finally:
            await client.aclose()

    assert calls["n"] >= 1
    assert governor.emitted >= 1


@pytest.mark.asyncio
async def test_two_governor_instances_share_the_admission_quota(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    first = _governor(clock, clean_cal_keys, cal_namespace)
    second = _governor(clock, clean_cal_keys, cal_namespace)

    await first.admit_book("passe-1")
    await second.admit_book("passe-1")
    with pytest.raises(CalSuspendedError) as denied:
        await first.admit_book("passe-1")
    assert denied.value.reason == "pass_budget"


@pytest.mark.asyncio
async def test_two_governor_instances_share_the_single_probe(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    first = _governor(clock, clean_cal_keys, cal_namespace)
    second = _governor(clock, clean_cal_keys, cal_namespace)
    await first.suspend("bot_challenge", 3600)
    clock.now += 3601

    await first.admit_book("passe-1")
    with pytest.raises(CalSuspendedError) as denied:
        await second.admit_book("passe-2")
    assert denied.value.reason == "probe_in_flight"


@pytest.mark.asyncio
async def test_an_unavailable_store_never_allows_ungoverned_calls(
    clean_cal_keys, cal_namespace,  # noqa: ARG001
):
    class BrokenStore:
        async def eval(self, *_args, **_kwargs):
            raise RuntimeError("redis down")

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"offers": {}})

    governor = _governor(FakeClock(), BrokenStore(), cal_namespace,
                         max_requests_per_collection=1)
    async with _client(handler) as client:
        wrapped = GovernedClient(client, CalCollectionBudget(governor))
        with pytest.raises(CalSuspendedError) as denied:
            await wrapped.get(BASE + "/search?query=x")

    assert denied.value.reason == "storage_unavailable"
    assert calls["n"] == 0
    assert governor.storage_unavailable is True


@pytest.mark.asyncio
async def test_polling_a_collection_is_not_spaced_by_the_inter_collection_interval(
    clean_cal_keys, cal_namespace,
):
    from app.providers.base import requested_components
    from app.providers.chasse_aux_livres import (
        LOOKUP_POLL_BUDGET_SECONDS,
        LOOKUP_POLL_MAX,
        ChasseAuxLivresProvider,
    )

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=(
                '<div data-asin="2756079332" data-lvs="nanbanjin" data-nbeng="2"></div>'
            ))
        return httpx.Response(200, json={"offers": {"0": [{
            "logo": {"name": "MOMOX_SHOP"},
            "condition": {"_name": "USED"},
            "subCondition": {"name": "Très bon"},
            "totalPrice": {"amount": 1499},
            "affiliateUrl": "https://example.com/momox",
        }]}})

    clock = FakeClock()
    governor = _governor(clock, clean_cal_keys, cal_namespace)
    assert governor.min_interval_seconds == CAL_MIN_INTERVAL_SECONDS
    assert governor.min_interval_seconds > 0

    scope = requested_components.set(frozenset({"current"}))
    started = time.monotonic()
    try:
        async with _client(handler) as client:
            offers = await ChasseAuxLivresProvider(
                http_client=client, sleep=AsyncMock(), governor=governor,
            ).get_offers(
                isbn13="9782756079332", title="Nanbanjin",
                source_url=BASE + "/prix/2756079332/9782756079332",
            )
    finally:
        requested_components.reset(scope)
    elapsed = time.monotonic() - started

    attempt = offers.attempts[0]
    assert attempt.poll_count == LOOKUP_POLL_MAX
    assert attempt.status == "partial"
    assert attempt.engines_received < attempt.engines_requested
    assert governor.emitted == 2 + LOOKUP_POLL_MAX
    assert governor.emitted <= governor.max_requests_per_collection
    assert elapsed < 2 * LOOKUP_POLL_BUDGET_SECONDS
    assert elapsed < CAL_MIN_INTERVAL_SECONDS


@pytest.mark.asyncio
async def test_the_inter_collection_interval_still_separates_two_collections(
    clean_cal_keys, cal_namespace,
):
    clock = FakeClock()
    governor = _governor(clock, clean_cal_keys, cal_namespace)
    observed: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        observed.append(seconds)
        clock.now += seconds

    governor.sleep = recording_sleep

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html></html>")

    async with _client(handler) as client:
        await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/prix/1")
        await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/prix/2")

    assert observed
    assert sum(observed) >= CAL_MIN_INTERVAL_SECONDS
    assert governor.emitted == 2
