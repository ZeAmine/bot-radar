"""Régulation des requêtes CAL : budget, concurrence, espacement, coupe-circuit."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.providers.cal_governor import (
    CAL_MAX_REQUESTS_PER_COLLECTION,
    CAL_MAX_REQUESTS_PER_WINDOW,
    CLOSED,
    OPEN,
    PROBING,
    CalCollectionBudget,
    CalGovernor,
    CalSuspendedError,
    GovernedClient,
    retry_after_seconds,
)
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider

BASE = "https://www.chasse-aux-livres.fr"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _governor(clock: FakeClock, **kwargs) -> tuple[CalGovernor, list[float]]:
    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)
        clock.now += delay

    options = {
        "store": False,
        "clock": clock,
        "wall_clock": lambda: datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
        "sleep": sleep,
    }
    options.update(kwargs)
    return CalGovernor(**options), slept


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_a_pass_of_eight_books_stays_within_the_cal_budget():
    clock = FakeClock()
    governor, _ = _governor(clock)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"offers": {}})

    async with _client(handler) as client:
        for _ in range(8):
            budget = CalCollectionBudget(governor)
            wrapped = GovernedClient(client, budget)
            await wrapped.get(BASE + "/search?query=x")
            await wrapped.get(BASE + "/search?query=x")

    assert calls["n"] == 16
    assert governor.emitted == 16
    assert len(governor.state.window) <= CAL_MAX_REQUESTS_PER_WINDOW


@pytest.mark.asyncio
async def test_collection_budget_counts_every_request_including_polls():
    clock = FakeClock()
    governor, _ = _governor(clock, max_requests_per_collection=3)
    budget = CalCollectionBudget(governor)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with _client(handler) as client:
        wrapped = GovernedClient(client, budget)
        for _ in range(3):
            await wrapped.get(BASE + "/rest/lookup/results")

    assert budget.used == 3
    with pytest.raises(CalSuspendedError) as denied:
        async with _client(lambda _r: httpx.Response(200)) as client:
            await GovernedClient(client, budget).get(BASE + "/rest/lookup/results")
    assert denied.value.reason == "collection_budget"


@pytest.mark.asyncio
async def test_window_limit_bounds_requests_over_time():
    clock = FakeClock()
    governor, slept = _governor(clock, max_requests_per_window=2,
                                max_wait_seconds=120.0)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with _client(handler) as client:
        wrapped = GovernedClient(client, CalCollectionBudget(governor))
        await wrapped.get(BASE + "/a")
        await wrapped.get(BASE + "/b")
        await wrapped.get(BASE + "/c")

    assert governor.emitted == 3
    assert slept
    assert slept[0] >= governor.window_seconds - 1e-6


@pytest.mark.asyncio
async def test_challenge_opens_the_breaker_and_blocks_pending_calls():
    clock = FakeClock()
    governor, _ = _governor(clock)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Veuillez patienter, vérification robot")

    async with _client(handler) as client:
        await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/search")

    assert governor.is_open()
    assert governor.state.reason == "bot_challenge"
    assert governor.retry_at() is not None

    async with _client(lambda _r: httpx.Response(200)) as client:
        with pytest.raises(CalSuspendedError) as denied:
            await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/search")
    assert denied.value.reason == "bot_challenge"


@pytest.mark.asyncio
async def test_concurrent_calls_cannot_all_pass_a_challenge():
    clock = FakeClock()
    governor, _ = _governor(clock)
    admitted = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        admitted["n"] += 1
        return httpx.Response(200, text="vérification robot")

    async with _client(handler) as client:
        await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/a")

    results = []
    async with _client(lambda _r: httpx.Response(200)) as client:
        for _ in range(5):
            try:
                await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/b")
                results.append("admitted")
            except CalSuspendedError:
                results.append("denied")
    assert results == ["denied"] * 5


@pytest.mark.asyncio
async def test_rate_limited_respects_retry_after():
    clock = FakeClock()
    governor, _ = _governor(clock)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with _client(handler) as client:
        await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/a")

    assert governor.state.reason == "rate_limited"
    assert governor.state.open_until - clock.now == pytest.approx(120, abs=1)


@pytest.mark.asyncio
async def test_rate_limited_without_header_uses_prudent_delay():
    clock = FakeClock()
    governor, _ = _governor(clock)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    async with _client(handler) as client:
        await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/a")

    assert governor.state.open_until - clock.now >= 600


def test_retry_after_parsing_ignores_invalid_values():
    def response(value: str | None) -> httpx.Response:
        headers = {"Retry-After": value} if value is not None else {}
        return httpx.Response(429, headers=headers)

    assert retry_after_seconds(response("30")) == 30
    assert retry_after_seconds(response("0")) is None
    assert retry_after_seconds(response("-5")) is None
    assert retry_after_seconds(response("soon")) is None
    assert retry_after_seconds(response(None)) is None


@pytest.mark.asyncio
async def test_spacing_serialises_collection_starts():
    clock = FakeClock()
    governor, slept = _governor(clock, min_interval_seconds=2.0)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with _client(handler) as client:
        for _ in range(3):
            await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/a")

    assert slept == [pytest.approx(2.0)] * 2
    assert governor.throttled == 2


@pytest.mark.asyncio
async def test_unavailable_store_does_not_allow_unlimited_calls():
    class BrokenStore:
        async def get(self, _key):
            raise RuntimeError("redis down")

        async def set(self, _key, _value):
            raise RuntimeError("redis down")

    clock = FakeClock()
    governor, _ = _governor(clock, store=BrokenStore(),
                            max_requests_per_collection=2)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    budget = CalCollectionBudget(governor)
    async with _client(handler) as client:
        wrapped = GovernedClient(client, budget)
        await wrapped.get(BASE + "/a")
        await wrapped.get(BASE + "/b")
        with pytest.raises(CalSuspendedError):
            await wrapped.get(BASE + "/c")

    assert governor.storage_unavailable is True


@pytest.mark.asyncio
async def test_suspension_survives_a_new_governor_instance_through_the_store():
    class MemoryStore:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}

        async def get(self, key):
            return self.data.get(key)

        async def set(self, key, value):
            self.data[key] = value

    store = MemoryStore()
    clock = FakeClock()
    first, _ = _governor(clock, store=store)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="vérification robot")

    async with _client(handler) as client:
        await GovernedClient(client, CalCollectionBudget(first)).get(BASE + "/a")

    second, _ = _governor(clock, store=store)

    async with _client(lambda _r: httpx.Response(200)) as client:
        with pytest.raises(CalSuspendedError):
            await GovernedClient(client, CalCollectionBudget(second)).get(BASE + "/a")


@pytest.mark.asyncio
async def test_breaker_closes_after_the_rest_period():
    clock = FakeClock()
    governor, _ = _governor(clock)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="vérification robot")

    async with _client(handler) as client:
        await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/a")
    assert governor.is_open()

    clock.now += 6 * 3600 + 1
    assert not governor.is_open()
    assert governor.retry_at() is None


def test_default_budget_is_bounded_and_explicit():
    governor = CalGovernor(store=False)
    assert governor.max_concurrent == 1
    assert governor.max_requests_per_collection == CAL_MAX_REQUESTS_PER_COLLECTION
    assert governor.max_requests_per_window == CAL_MAX_REQUESTS_PER_WINDOW
    assert governor.window_seconds == 60.0


@pytest.mark.asyncio
async def test_deferred_collection_is_not_a_business_failure():
    from app.providers.chasse_aux_livres import ChasseAuxLivresProvider

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="vérification robot")

    governor = CalGovernor(store=False, min_interval_seconds=0.0)
    async with _client(handler) as client:
        provider = ChasseAuxLivresProvider(
            http_client=client, max_retries=0, governor=governor,
        )
        await provider.get_offers(isbn13="9782035951463", title="Premier")
        emitted = calls["n"]
        deferred = await provider.get_offers(isbn13="9782035951462", title="Second")

    assert calls["n"] == emitted
    attempt = deferred.attempts[0]
    assert attempt.status == "skipped"
    assert attempt.reason == "bot_challenge"
    assert attempt.retry_after_seconds is not None
    assert attempt.retry_after_seconds > 0
    assert deferred.failed_providers == ()
    assert list(deferred) == []


@pytest.mark.asyncio
async def test_deferral_does_not_renew_the_session():
    from unittest.mock import AsyncMock

    from app.providers import chasse_aux_livres as module

    renew = AsyncMock(return_value="nouveau-token")
    governor = CalGovernor(store=False, min_interval_seconds=0.0)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="vérification robot")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module.chasse_session, "_login", renew)
        async with _client(handler) as client:
            provider = module.ChasseAuxLivresProvider(
                http_client=client, max_retries=0, governor=governor,
            )
            await provider.get_offers(isbn13="9782035951463", title="Premier")
            await provider.get_offers(isbn13="9782035951462", title="Second")

    assert renew.await_count == 0


def test_governed_client_exposes_the_underlying_client():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    inner = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    governor = CalGovernor(store=False)
    wrapped = GovernedClient(inner, CalCollectionBudget(governor))
    assert wrapped.timeout is inner.timeout


@pytest.mark.asyncio
async def test_a_pass_admits_at_most_two_cal_books():
    from app.providers.cal_governor import pass_occurrence

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"offers": {}})

    governor = CalGovernor(store=False, min_interval_seconds=0.0)
    token = pass_occurrence.set("passe-unique")
    try:
        async with _client(handler) as client:
            provider = ChasseAuxLivresProvider(
                http_client=client, max_retries=0, governor=governor,
            )
            results = [await provider.get_offers(isbn13="9782035951463", title=f"T{i}")
                       for i in range(5)]
    finally:
        pass_occurrence.reset(token)

    reasons = [r.attempts[0].reason for r in results]
    assert reasons.count("pass_budget") == 3
    assert governor.state.admitted_books == 2
    assert governor.deferred == 3


@pytest.mark.asyncio
async def test_a_new_pass_gets_its_own_admission_quota():

    governor = CalGovernor(store=False, min_interval_seconds=0.0)
    await governor.admit_book("passe-1")
    await governor.admit_book("passe-1")
    await governor.admit_book("passe-2")
    assert governor.state.admitted_pass == "passe-2"
    assert governor.state.admitted_books == 1


@pytest.mark.asyncio
async def test_probe_state_admits_a_single_collection():
    clock = FakeClock()
    governor, _ = _governor(clock)
    await governor.suspend("bot_challenge", 3600)
    clock.now += 3601

    await governor.admit_book("passe-1")
    assert governor.phase() == PROBING
    with pytest.raises(CalSuspendedError) as same:
        await governor.admit_book("passe-1")
    assert same.value.reason == "probe_in_flight"
    with pytest.raises(CalSuspendedError) as other:
        await governor.admit_book("passe-2")
    assert other.value.reason == "probe_in_flight"


@pytest.mark.asyncio
async def test_a_successful_probe_closes_the_circuit_and_restores_the_quota():
    clock = FakeClock()
    governor, _ = _governor(clock)
    await governor.suspend("bot_challenge", 3600)
    clock.now += 3601
    await governor.admit_book("passe-1")
    await governor.conclude_probe(succeeded=True)

    assert governor.phase() == CLOSED
    assert governor.state.admitted_books == 0
    await governor.admit_book("passe-1")
    await governor.admit_book("passe-1")


@pytest.mark.asyncio
async def test_an_incomplete_probe_never_restores_full_admission():
    clock = FakeClock()
    governor, _ = _governor(clock)
    await governor.suspend("bot_challenge", 3600)
    clock.now += 3601
    await governor.admit_book("passe-1")
    await governor.conclude_probe(succeeded=False, reason="partial")

    assert governor.phase() == OPEN
    with pytest.raises(CalSuspendedError):
        await governor.admit_book("passe-2")


@pytest.mark.asyncio
async def test_a_challenge_during_the_probe_reopens_for_six_hours():
    clock = FakeClock()
    governor, _ = _governor(clock)
    await governor.suspend("bot_challenge", 3600)
    clock.now += 3601
    await governor.admit_book("passe-1")
    await governor.conclude_probe(succeeded=False, reason="bot_challenge")

    assert governor.state.open_until - clock.now == pytest.approx(6 * 3600, abs=1)


@pytest.mark.asyncio
async def test_a_lost_probe_expires_without_blocking_forever():
    clock = FakeClock()
    governor, _ = _governor(clock, probe_ttl_seconds=60.0)
    await governor.suspend("bot_challenge", 3600)
    clock.now += 3601
    await governor.admit_book("passe-1")
    assert governor.phase() == PROBING

    clock.now += 61
    assert governor.phase() == CLOSED
    await governor.admit_book("passe-2")


@pytest.mark.asyncio
async def test_a_probe_abandoned_by_process_death_is_not_replayed_in_a_burst():
    class MemoryStore:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}

        async def get(self, key):
            return self.data.get(key)

        async def set(self, key, value):
            self.data[key] = value

    store = MemoryStore()
    clock = FakeClock()
    governor, _ = _governor(clock, store=store)
    await governor.suspend("bot_challenge", 3600)
    clock.now += 3601
    await governor.admit_book("passe-1")

    revived, _ = _governor(clock, store=store)
    with pytest.raises(CalSuspendedError) as denied:
        await revived.admit_book("passe-2")
    assert denied.value.reason == "probe_in_flight"


@pytest.mark.asyncio
async def test_a_governed_client_is_never_wrapped_twice():
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200)

    governor = CalGovernor(store=False, min_interval_seconds=0.0)
    async with _client(handler) as inner:
        outer = GovernedClient(inner, CalCollectionBudget(governor))
        nested = GovernedClient(outer, CalCollectionBudget(governor))
        await asyncio.wait_for(nested.get(BASE + "/prix/x"), timeout=5)

    assert calls["n"] == 1
    assert governor.emitted == 1
