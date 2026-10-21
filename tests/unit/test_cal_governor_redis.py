"""Atomicité du régulateur CAL contre une instance Redis de test réelle.

Ces tests exercent plusieurs instances du régulateur sur les mêmes clés, en
parallèle : ils ne prouvent quelque chose que parce que le serveur Redis
sérialise réellement les scripts Lua. Sans ``CAL_TEST_REDIS_URL`` ils sont
ignorés, et aucun double mémoire ne les remplace.

Aucun accès à un Redis de production : l'URL vient uniquement de
l'environnement de test, sur une instance jetable.
"""

import asyncio

import httpx
import pytest

from app.providers.cal_governor import (
    CalCollectionBudget,
    CalGovernor,
    CalSuspendedError,
    GovernedClient,
)
from tests.unit.cal_redis_fixture import CAL_TEST_REDIS_ENV

pytest_plugins = ["tests.unit.cal_redis_fixture"]

BASE = "https://www.chasse-aux-livres.fr"


class _SharedClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def _governor(store, namespace: str, **kwargs) -> CalGovernor:
    options = {"store": store, "namespace": namespace, "min_interval_seconds": 0.0}
    clock = kwargs.pop("clock", None)
    if clock is not None:
        options["clock"] = clock
    options.update({key: value for key, value in kwargs.items() if value is not None})
    return CalGovernor(**options)


async def _admissions(governors, pass_id: str) -> list[str]:
    async def attempt(governor: CalGovernor) -> str:
        try:
            await governor.admit_book(pass_id)
        except CalSuspendedError as exc:
            return exc.reason
        return "admitted"

    return list(await asyncio.gather(*(attempt(governor) for governor in governors)))


@pytest.mark.asyncio
async def test_simultaneous_admissions_never_exceed_the_shared_quota(cal_redis, cal_namespace):
    governors = [_governor(cal_redis, cal_namespace) for _ in range(12)]

    outcomes = await _admissions(governors, "passe-1")

    assert outcomes.count("admitted") == 2
    assert outcomes.count("pass_budget") == 10


@pytest.mark.asyncio
async def test_simultaneous_admissions_across_distinct_passes_stay_bounded(
    cal_redis, cal_namespace,
):
    governors = [_governor(cal_redis, cal_namespace) for _ in range(8)]

    async def attempt(index: int, governor: CalGovernor) -> str:
        try:
            await governor.admit_book(f"passe-{index}")
        except CalSuspendedError as exc:
            return exc.reason
        return "admitted"

    outcomes = await asyncio.gather(*(attempt(i, g) for i, g in enumerate(governors)))
    assert list(outcomes).count("admitted") == 2


@pytest.mark.asyncio
async def test_concurrency_is_never_exceeded_across_instances(cal_redis, cal_namespace):
    class Tracker:
        def __init__(self) -> None:
            self.active = 0
            self.peak = 0

        async def handle(self, _request: httpx.Request) -> httpx.Response:
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return httpx.Response(200)

    tracker = Tracker()
    governors = [
        _governor(cal_redis, cal_namespace, max_wait_seconds=10.0) for _ in range(6)
    ]

    async def run(governor: CalGovernor) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(tracker.handle)) as client:
            await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/a")

    await asyncio.gather(*(run(governor) for governor in governors))

    assert tracker.peak == 1


@pytest.mark.asyncio
async def test_only_one_probe_is_admitted_after_the_rest(cal_redis, cal_namespace):
    seed = _governor(cal_redis, cal_namespace)
    await seed.suspend("bot_challenge", 1)
    await asyncio.sleep(1.1)

    governors = [_governor(cal_redis, cal_namespace) for _ in range(6)]
    outcomes = await _admissions(governors, "passe-1")

    assert outcomes.count("admitted") == 1
    assert outcomes.count("probe_in_flight") == 5


@pytest.mark.asyncio
async def test_a_concurrent_restriction_is_not_erased_by_a_collection_ending(
    cal_redis, cal_namespace,
):
    governor, other = _governor(cal_redis, cal_namespace), _governor(cal_redis, cal_namespace)

    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200)

    async def collect() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/a")

    collecting = asyncio.create_task(collect())
    await asyncio.sleep(0.01)
    await other.suspend("bot_challenge", 3600)
    await collecting

    snapshot = await governor.snapshot()
    assert snapshot.reason == "bot_challenge"
    assert governor.phase() == "open"


@pytest.mark.asyncio
async def test_an_abandoned_lease_expires_and_stops_blocking(cal_redis, cal_namespace):
    stuck = _governor(cal_redis, cal_namespace, lease_ttl_seconds=1.0, max_wait_seconds=0.05)

    async def slowish(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slowish)):
        budget = CalCollectionBudget(stuck)
        await stuck._acquire_slot(budget)

        blocked = await _admissions(
            [_governor(cal_redis, cal_namespace, max_wait_seconds=0.05)], "passe-1",
        )
        assert blocked == ["admitted"]

        with pytest.raises(CalSuspendedError):
            await _governor(
                cal_redis, cal_namespace, max_wait_seconds=0.05,
            )._acquire_slot(CalCollectionBudget(_governor(cal_redis, cal_namespace)))

        await asyncio.sleep(1.1)
        await stuck._acquire_slot(CalCollectionBudget(stuck))
        assert await cal_redis.zcard(stuck.lease_key) == 1


@pytest.mark.asyncio
async def test_an_unavailable_store_blocks_every_instance(
    cal_redis, cal_namespace,  # noqa: ARG001
):
    class BrokenStore:
        async def eval(self, *_args, **_kwargs):
            raise ConnectionError("redis down")

    broken = [_governor(BrokenStore(), cal_namespace) for _ in range(3)]
    outcomes = await _admissions(broken, "passe-1")
    assert outcomes == ["storage_unavailable"] * 3


def test_the_redis_url_comes_from_the_test_environment_only():
    assert CAL_TEST_REDIS_ENV == "CAL_TEST_REDIS_URL"


@pytest.mark.asyncio
async def test_the_admission_wait_is_bounded_and_then_refused(cal_redis, cal_namespace):
    holder = _governor(
        cal_redis, cal_namespace, max_concurrent=1, min_interval_seconds=0.0,
        max_wait_seconds=0.3,
    )
    waiter = _governor(
        cal_redis, cal_namespace, max_concurrent=1, min_interval_seconds=0.0,
        max_wait_seconds=0.3,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)):
        await holder._acquire_slot(CalCollectionBudget(holder))
        started = asyncio.get_running_loop().time()
        with pytest.raises(CalSuspendedError):
            await waiter._acquire_slot(CalCollectionBudget(waiter))
        elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.5
    assert waiter.denied == 1


@pytest.mark.asyncio
async def test_a_user_trigger_outside_any_pass_shares_the_same_budget(cal_redis, cal_namespace):
    scheduled = _governor(cal_redis, cal_namespace)
    greeted = _governor(cal_redis, cal_namespace)

    await scheduled.admit_book("passe-2026-10-21T12:15")
    await scheduled.admit_book("passe-2026-10-21T12:15")
    with pytest.raises(CalSuspendedError) as denied:
        await greeted.admit_book(None)

    assert denied.value.reason == "pass_budget"


@pytest.mark.asyncio
async def test_concurrent_collections_and_maintenance_share_one_serialised_lane(
    cal_redis, cal_namespace,
):
    peak = {"active": 0, "max": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        peak["active"] += 1
        peak["max"] = max(peak["max"], peak["active"])
        await asyncio.sleep(0.02)
        peak["active"] -= 1
        return httpx.Response(200)

    collection = _governor(
        cal_redis, cal_namespace, max_wait_seconds=10.0, min_interval_seconds=0.0,
    )
    maintenance = _governor(
        cal_redis, cal_namespace, max_wait_seconds=10.0, min_interval_seconds=0.0,
    )

    async def lane(governor: CalGovernor, pass_id: str, times: int) -> None:
        await governor.admit_book(pass_id)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            wrapped = GovernedClient(client, CalCollectionBudget(governor))
            for _ in range(times):
                await wrapped.get(BASE + "/a")

    await asyncio.gather(
        lane(collection, "passe-1", 4),
        lane(maintenance, "hors-passe", 3),
    )

    assert peak["max"] == 1
    assert collection.emitted + maintenance.emitted == 7


@pytest.mark.asyncio
async def test_a_successful_probe_never_erases_a_newer_restriction(cal_redis, cal_namespace):
    clock = _SharedClock()
    governor = _governor(cal_redis, cal_namespace, clock=clock, probe_ttl_seconds=600.0)
    await governor.suspend("bot_challenge", 1)
    clock.advance(1.1)

    token = await governor.admit_book("passe-1")
    assert token is not None

    await governor.suspend("bot_challenge", 3600)
    before = (await governor.snapshot()).open_until

    outcome = await governor.conclude_probe(token, succeeded=True)

    assert outcome == "ignored"
    assert (await governor.snapshot()).open_until == before
    assert governor.phase() == "open"


@pytest.mark.asyncio
async def test_an_older_collection_cannot_conclude_someone_elses_probe(cal_redis, cal_namespace):
    clock = _SharedClock()
    governor = _governor(cal_redis, cal_namespace, clock=clock, probe_ttl_seconds=600.0)
    await governor.suspend("bot_challenge", 1)
    clock.advance(1.1)

    token = await governor.admit_book("passe-1")
    outcome = await governor.conclude_probe("jeton-etranger", succeeded=True)

    assert outcome == "ignored"
    assert governor.phase() == "probing"
    assert (await governor.snapshot()).probe_pass == "passe-1"

    assert await governor.conclude_probe(token, succeeded=True) == "closed"


@pytest.mark.asyncio
async def test_an_old_token_cannot_close_a_probe_it_did_not_hold(
    cal_redis, cal_namespace,
):
    clock = _SharedClock()
    governor = _governor(cal_redis, cal_namespace, clock=clock, probe_ttl_seconds=600.0)
    await governor.suspend("bot_challenge", 1)
    clock.advance(1.1)

    old_token = await governor.admit_book("passe-1")
    assert old_token is not None
    assert await governor.conclude_probe(old_token, succeeded=True) == "closed"

    await governor.suspend("bot_challenge", 1)
    clock.advance(1.1)
    new_token = await governor.admit_book("passe-2")
    assert new_token is not None

    assert await governor.conclude_probe(old_token, succeeded=True) == "ignored"
    assert governor.phase() == "probing"
    assert await governor.conclude_probe(new_token, succeeded=True) == "closed"


@pytest.mark.asyncio
async def test_an_interrupted_probe_does_not_close_the_circuit(cal_redis, cal_namespace):
    clock = _SharedClock()
    governor = _governor(cal_redis, cal_namespace, clock=clock)
    await governor.suspend("bot_challenge", 1)
    clock.advance(1.1)

    token = await governor.admit_book("passe-1")
    assert await governor.conclude_probe(token, succeeded=False, reason="pass_budget") != "closed"
    assert governor.phase() == "open"


@pytest.mark.asyncio
async def test_a_probe_consumes_an_admission_slot(cal_redis, cal_namespace):
    clock = _SharedClock()
    governor = _governor(cal_redis, cal_namespace, clock=clock, max_books_per_pass=2)
    await governor.suspend("bot_challenge", 1)
    clock.advance(1.1)

    token = await governor.admit_book("passe-1")
    assert token is not None
    await governor.conclude_probe(token, succeeded=True)

    await governor.admit_book("passe-1")
    with pytest.raises(CalSuspendedError):
        await governor.admit_book("passe-1")


@pytest.mark.asyncio
async def test_maintenance_and_user_triggers_do_not_consume_book_admissions(
    cal_redis, cal_namespace,
):
    governor = _governor(cal_redis, cal_namespace, max_books_per_pass=2)

    await governor.admit_book("maintenance", counts_as_book=False)
    await governor.admit_book(None, counts_as_book=False)

    await governor.admit_book("passe-1")
    await governor.admit_book("passe-1")
    with pytest.raises(CalSuspendedError) as denied:
        await governor.admit_book("passe-1")
    assert denied.value.reason == "pass_budget"


@pytest.mark.asyncio
async def test_a_script_consumes_no_slot_from_the_books_pass(cal_redis, cal_namespace):
    governor = _governor(cal_redis, cal_namespace, max_books_per_pass=2)

    await governor.admit_book("script-chasse-prices", counts_as_book=False)
    await governor.admit_book("script-diagnostic", counts_as_book=False)

    assert await governor.admit_book("passe-1") is None
    assert await governor.admit_book("passe-1") is None
