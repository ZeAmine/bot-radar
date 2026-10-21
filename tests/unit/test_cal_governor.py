"""Régulation des requêtes CAL : quota, concurrence, espacement, coupe-circuit.

Les décisions passent par des scripts Lua exécutés par Redis. Les tests qui
portent sur ces décisions tournent donc contre une instance Redis de test
fournie par ``CAL_TEST_REDIS_URL`` ; sans elle, ils sont ignorés plutôt que
remplacés par un double, qui ne prouverait rien de l'atomicité.
"""

import asyncio

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

pytest_plugins = ["tests.unit.cal_redis_fixture"]

BASE = "https://www.chasse-aux-livres.fr"

class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def _governor(
    clock: FakeClock, store, namespace: str, **kwargs,
) -> tuple[CalGovernor, list[float]]:
    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)
        clock.now += delay

    options = {"store": store, "clock": clock, "sleep": sleep, "namespace": namespace}
    options.update(kwargs)
    return CalGovernor(**options), slept


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_a_pass_admits_at_most_two_cal_books(clean_cal_keys, cal_namespace):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"offers": {}})

    governor, _ = _governor(FakeClock(), clean_cal_keys, cal_namespace)
    async with _client(handler) as client:
        provider = ChasseAuxLivresProvider(
            http_client=client, max_retries=0, governor=governor,
        )
        results = [
            await provider.get_offers(isbn13="9782035951463", title=f"T{index}")
            for index in range(5)
        ]

    reasons = [result.attempts[0].reason for result in results]
    assert reasons.count("pass_budget") == 3
    assert calls["n"] <= governor.max_requests_per_collection * 2


@pytest.mark.asyncio
async def test_a_new_pass_cannot_reload_the_quota_inside_the_window(clean_cal_keys, cal_namespace):
    governor, _ = _governor(FakeClock(), clean_cal_keys, cal_namespace)
    await governor.admit_book("passe-1")
    await governor.admit_book("passe-1")
    with pytest.raises(CalSuspendedError) as denied:
        await governor.admit_book("passe-2")
    assert denied.value.reason == "pass_budget"


@pytest.mark.asyncio
async def test_the_outside_pass_budget_expires_on_its_own_window(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    governor, _ = _governor(clock, clean_cal_keys, cal_namespace)
    await governor.admit_book(None)
    await governor.admit_book(None)
    with pytest.raises(CalSuspendedError) as denied:
        await governor.admit_book(None)
    assert denied.value.reason == "pass_budget"

    clock.now += governor.pass_window_seconds + 1
    await governor.admit_book(None)


@pytest.mark.asyncio
async def test_changing_the_pass_identifier_cannot_bypass_the_quota(clean_cal_keys, cal_namespace):
    governor, _ = _governor(FakeClock(), clean_cal_keys, cal_namespace, max_books_per_pass=2)
    await governor.admit_book("passe-1")
    await governor.admit_book("passe-1")
    with pytest.raises(CalSuspendedError) as denied:
        await governor.admit_book("passe-3")
    assert denied.value.reason == "pass_budget"


@pytest.mark.asyncio
async def test_collection_budget_counts_every_request(clean_cal_keys, cal_namespace):
    governor, _ = _governor(
        FakeClock(), clean_cal_keys, cal_namespace, max_requests_per_collection=3,
    )
    budget = CalCollectionBudget(governor)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with _client(handler) as client:
        wrapped = GovernedClient(client, budget)
        for _ in range(3):
            await wrapped.get(BASE + "/rest/lookup/results")
        with pytest.raises(CalSuspendedError) as denied:
            await wrapped.get(BASE + "/rest/lookup/results")

    assert budget.used == 3
    assert denied.value.reason == "collection_budget"


@pytest.mark.asyncio
async def test_window_limit_bounds_requests_over_time(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    governor, slept = _governor(
        clock, clean_cal_keys, cal_namespace, max_requests_per_window=2,
        max_wait_seconds=120.0, min_interval_seconds=0.0,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with _client(handler) as client:
        wrapped = GovernedClient(client, CalCollectionBudget(governor))
        for path in ("/a", "/b", "/c"):
            await wrapped.get(BASE + path)

    assert governor.emitted == 3
    assert sum(slept) >= governor.window_seconds - 1.5


@pytest.mark.asyncio
async def test_challenge_opens_the_breaker_and_blocks_later_calls(clean_cal_keys, cal_namespace):
    governor, _ = _governor(FakeClock(), clean_cal_keys, cal_namespace)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Veuillez patienter, vérification robot")

    async with _client(handler) as governed:
        await GovernedClient(governed, CalCollectionBudget(governor)).get(BASE + "/search")

    await governor.snapshot()
    assert governor.phase() == OPEN

    async with _client(lambda _r: httpx.Response(200)) as client:
        with pytest.raises(CalSuspendedError) as denied:
            await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/search")
    assert denied.value.reason == "bot_challenge"


@pytest.mark.asyncio
async def test_rate_limited_respects_retry_after(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    governor, _ = _governor(clock, clean_cal_keys, cal_namespace)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with _client(handler) as client:
        await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/a")

    snapshot = await governor.snapshot()
    assert snapshot.reason == "rate_limited"
    assert snapshot.open_until - clock.now == pytest.approx(120, abs=2)


@pytest.mark.asyncio
async def test_rate_limited_without_header_uses_prudent_delay(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    governor, _ = _governor(clock, clean_cal_keys, cal_namespace)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    async with _client(handler) as client:
        await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/a")

    snapshot = await governor.snapshot()
    assert snapshot.open_until - clock.now >= 600


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
async def test_spacing_serialises_collection_starts(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    governor, slept = _governor(
        clock, clean_cal_keys, cal_namespace, min_interval_seconds=2.0,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with _client(handler) as client:
        for _ in range(2):
            await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/a")

    assert sum(slept) == pytest.approx(2.0, abs=0.3)
    assert governor.throttled >= 1


@pytest.mark.asyncio
async def test_probe_state_admits_a_single_collection(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    governor, _ = _governor(clock, clean_cal_keys, cal_namespace)
    await governor.suspend("bot_challenge", 3600)
    clock.now += 3601

    await governor.admit_book("passe-1")
    assert governor.phase() == PROBING
    with pytest.raises(CalSuspendedError) as denied:
        await governor.admit_book("passe-2")
    assert denied.value.reason == "probe_in_flight"


@pytest.mark.asyncio
async def test_a_successful_probe_closes_the_circuit(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    governor, _ = _governor(clock, clean_cal_keys, cal_namespace)
    await governor.suspend("bot_challenge", 3600)
    clock.now += 3601
    token = await governor.admit_book("passe-1")
    await governor.conclude_probe(token, succeeded=True)

    assert governor.phase() == CLOSED
    assert await governor.admit_book("passe-1") is None


@pytest.mark.asyncio
async def test_a_probe_never_grants_an_extra_admission(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    governor, _ = _governor(clock, clean_cal_keys, cal_namespace, max_books_per_pass=2)
    await governor.suspend("bot_challenge", 3600)
    clock.now += 3601

    token = await governor.admit_book("passe-1")
    assert token is not None
    await governor.conclude_probe(token, succeeded=True)

    await governor.admit_book("passe-1")
    with pytest.raises(CalSuspendedError) as denied:
        await governor.admit_book("passe-1")
    assert denied.value.reason == "pass_budget"


@pytest.mark.asyncio
async def test_an_incomplete_probe_never_restores_full_admission(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    governor, _ = _governor(clock, clean_cal_keys, cal_namespace)
    await governor.suspend("bot_challenge", 3600)
    clock.now += 3601
    token = await governor.admit_book("passe-1")
    await governor.conclude_probe(token, succeeded=False, reason="partial")

    assert governor.phase() == OPEN
    with pytest.raises(CalSuspendedError):
        await governor.admit_book("passe-2")


@pytest.mark.asyncio
async def test_a_challenge_during_the_probe_reopens_for_six_hours(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    governor, _ = _governor(clock, clean_cal_keys, cal_namespace)
    await governor.suspend("bot_challenge", 3600)
    clock.now += 3601
    token = await governor.admit_book("passe-1")
    await governor.conclude_probe(token, succeeded=False, reason="bot_challenge")

    snapshot = await governor.snapshot()
    assert snapshot.open_until - clock.now == pytest.approx(6 * 3600, abs=2)


@pytest.mark.asyncio
async def test_a_lost_probe_expires_without_blocking_forever(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    governor, _ = _governor(clock, clean_cal_keys, cal_namespace, probe_ttl_seconds=60.0)
    await governor.suspend("bot_challenge", 3600)
    clock.now += 3601
    await governor.admit_book("passe-1")
    assert governor.phase() == PROBING

    clock.now += 61
    assert governor.phase() == CLOSED
    await governor.admit_book("passe-2")


@pytest.mark.asyncio
async def test_a_newer_restriction_is_never_erased_by_an_older_collection(
    clean_cal_keys, cal_namespace,
):
    clock = FakeClock()
    governor, _ = _governor(
        clock, clean_cal_keys, cal_namespace, min_interval_seconds=0.0,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with _client(handler) as client:
        await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/a")

    await governor.suspend("bot_challenge", 3600)
    before = (await governor.snapshot()).open_until
    await governor.suspend("rate_limited", 60)
    assert (await governor.snapshot()).open_until == before


@pytest.mark.asyncio
async def test_releasing_one_lease_keeps_another_holder(clean_cal_keys, cal_namespace):  # noqa: ARG001
    governor, _ = _governor(FakeClock(), clean_cal_keys, cal_namespace, max_concurrent=2)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with _client(handler):
        first = CalCollectionBudget(governor)
        second = CalCollectionBudget(governor)
        token_a = await governor._acquire_slot(first)
        token_b = await governor._acquire_slot(second)
        await governor._release(token_a)
        assert await clean_cal_keys.zcard(governor.lease_key) == 1
        await governor._release(token_b)
        assert await clean_cal_keys.zcard(governor.lease_key) == 0


@pytest.mark.asyncio
async def test_an_abandoned_lease_expires_without_blocking_forever(clean_cal_keys, cal_namespace):
    clock = FakeClock()
    governor, _ = _governor(
        clock, clean_cal_keys, cal_namespace, max_concurrent=1, lease_ttl_seconds=30.0,
        min_interval_seconds=0.0, max_wait_seconds=0.05,
    )
    await governor._acquire_slot(CalCollectionBudget(governor))
    with pytest.raises(CalSuspendedError):
        await governor._acquire_slot(CalCollectionBudget(governor))

    clock.now += 31
    await governor._acquire_slot(CalCollectionBudget(governor))


@pytest.mark.asyncio
async def test_an_unavailable_store_never_allows_ungoverned_calls(
    clean_cal_keys, cal_namespace,  # noqa: ARG001
):
    class BrokenStore:
        async def eval(self, *_args, **_kwargs):
            raise RuntimeError("redis down")

    governor, _ = _governor(FakeClock(), BrokenStore(), cal_namespace)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with _client(handler) as client:
        with pytest.raises(CalSuspendedError) as denied:
            await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/a")

    assert denied.value.reason == "storage_unavailable"
    assert governor.storage_unavailable is True


@pytest.mark.asyncio
async def test_without_a_store_no_collection_is_admitted():
    governor = CalGovernor(store=False, min_interval_seconds=0.0)
    with pytest.raises(CalSuspendedError) as denied:
        await governor.admit_book("passe-1")
    assert denied.value.reason == "storage_unavailable"


def test_default_budget_is_bounded_and_explicit():
    governor = CalGovernor(store=False)
    assert governor.max_concurrent == 1
    assert governor.max_requests_per_collection == CAL_MAX_REQUESTS_PER_COLLECTION
    assert governor.max_requests_per_window == CAL_MAX_REQUESTS_PER_WINDOW
    assert governor.window_seconds == 60.0
    assert governor.max_books_per_pass == 2


@pytest.mark.asyncio
async def test_a_governed_client_is_never_wrapped_twice(clean_cal_keys, cal_namespace):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200)

    governor, _ = _governor(
        FakeClock(), clean_cal_keys, cal_namespace, min_interval_seconds=0.0,
    )
    async with _client(handler) as inner:
        outer = GovernedClient(inner, CalCollectionBudget(governor))
        nested = GovernedClient(outer, CalCollectionBudget(governor))
        await asyncio.wait_for(nested.get(BASE + "/prix/x"), timeout=5)

    assert calls["n"] == 1
    assert governor.emitted == 1
