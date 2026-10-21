"""Effets de bout en bout du régulateur CAL sur le parcours applicatif réel."""


import httpx
import pytest

from app.providers.cal_governor import (
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

BASE = "https://www.chasse-aux-livres.fr"
ISBN = "9782035951463"


class MemoryStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value):
        self.data[key] = value


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _governor(clock: FakeClock, **kwargs) -> CalGovernor:
    options = {"store": False, "clock": clock, "min_interval_seconds": 0.0}
    options.update(kwargs)
    return CalGovernor(**options)


@pytest.fixture
def pass_token():
    token = pass_occurrence.set("passe-e2e")
    yield token
    pass_occurrence.reset(token)


@pytest.mark.usefixtures("pass_token")
@pytest.mark.asyncio
async def test_a_deferral_before_any_call_is_not_a_provider_failure():
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"offers": {}})

    clock = FakeClock()
    governor = _governor(clock)
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


@pytest.mark.usefixtures("pass_token")
@pytest.mark.asyncio
async def test_exhausted_collection_budget_keeps_received_offers_as_partial():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/rest/lookup/results" in str(request.url):
            return httpx.Response(200, json={"offers": {"0": [
                {"price": 900, "shipping": 300, "url": "https://merchant.example/a",
                 "shop": "Boutique", "state": "Très bon état"},
            ]}})
        return httpx.Response(200, text="<html></html>")

    clock = FakeClock()
    governor = _governor(clock, max_requests_per_collection=2)
    async with _client(handler) as client:
        provider = ChasseAuxLivresProvider(
            http_client=client, max_retries=0, governor=governor,
        )
        offers = await provider.get_offers(isbn13=ISBN, title="Titre")

    current = [a for a in offers.attempts if a.operation == "current"]
    assert current
    assert current[0].status in {"partial", "failed"}
    assert governor.emitted <= 2


@pytest.mark.usefixtures("pass_token")
@pytest.mark.asyncio
async def test_a_challenge_during_a_poll_blocks_every_later_call():
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
    governor = _governor(clock)
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
async def test_maintenance_respects_an_open_circuit():
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="<html></html>")

    clock = FakeClock()
    governor = _governor(clock)
    await governor.suspend("bot_challenge", 3600)

    async def no_notification(_message: str) -> bool:
        return False

    session = ChasseSessionManager(
        store=MemoryStore(), notify=no_notification, governor=governor,
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
async def test_maintenance_is_admitted_once_the_circuit_is_closed():
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="<html></html>")

    governor = _governor(FakeClock())

    async def no_notification(_message: str) -> bool:
        return False

    session = ChasseSessionManager(
        store=MemoryStore(), notify=no_notification, governor=governor,
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
async def test_two_governor_instances_share_the_admission_quota():
    store = MemoryStore()
    clock = FakeClock()
    first = _governor(clock, store=store)
    second = _governor(clock, store=store)

    await first.admit_book("passe-1")
    await second.admit_book("passe-1")
    with pytest.raises(CalSuspendedError) as denied:
        await first.admit_book("passe-1")
    assert denied.value.reason == "pass_budget"


@pytest.mark.asyncio
async def test_two_governor_instances_share_the_single_probe():
    store = MemoryStore()
    clock = FakeClock()
    first = _governor(clock, store=store)
    second = _governor(clock, store=store)
    await first.suspend("bot_challenge", 3600)
    clock.now += 3601

    await first.admit_book("passe-1")
    with pytest.raises(CalSuspendedError) as denied:
        await second.admit_book("passe-2")
    assert denied.value.reason == "probe_in_flight"


@pytest.mark.asyncio
async def test_an_unavailable_store_never_allows_ungoverned_calls():
    class BrokenStore:
        async def get(self, _key):
            raise RuntimeError("redis down")

        async def set(self, _key, _value):
            raise RuntimeError("redis down")

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"offers": {}})

    governor = _governor(FakeClock(), store=BrokenStore(),
                         max_requests_per_collection=1)
    async with _client(handler) as client:
        wrapped = GovernedClient(client, CalCollectionBudget(governor))
        await wrapped.get(BASE + "/search?query=x")
        with pytest.raises(CalSuspendedError):
            await wrapped.get(BASE + "/search?query=y")

    assert calls["n"] == 1
    assert governor.storage_unavailable is True
