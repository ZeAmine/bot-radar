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
    CAL_CHALLENGE_REST_SECONDS,
    CAL_PROBE_REST_SECONDS,
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
async def test_simultaneous_admissions_never_exceed_the_shared_quota(clean_cal_keys, cal_namespace):
    governors = [_governor(clean_cal_keys, cal_namespace) for _ in range(12)]

    outcomes = await _admissions(governors, "passe-1")

    assert outcomes.count("admitted") == 2
    assert outcomes.count("pass_budget") == 10


@pytest.mark.asyncio
async def test_simultaneous_admissions_across_distinct_passes_stay_bounded(
    clean_cal_keys, cal_namespace,
):
    governors = [_governor(clean_cal_keys, cal_namespace) for _ in range(8)]

    async def attempt(index: int, governor: CalGovernor) -> str:
        try:
            await governor.admit_book(f"passe-{index}")
        except CalSuspendedError as exc:
            return exc.reason
        return "admitted"

    outcomes = await asyncio.gather(*(attempt(i, g) for i, g in enumerate(governors)))
    assert list(outcomes).count("admitted") == 2


@pytest.mark.asyncio
async def test_concurrency_is_never_exceeded_across_instances(clean_cal_keys, cal_namespace):
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
        _governor(clean_cal_keys, cal_namespace, max_wait_seconds=10.0) for _ in range(6)
    ]

    async def run(governor: CalGovernor) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(tracker.handle)) as client:
            await GovernedClient(client, CalCollectionBudget(governor)).get(BASE + "/a")

    await asyncio.gather(*(run(governor) for governor in governors))

    assert tracker.peak == 1


@pytest.mark.asyncio
async def test_only_one_probe_is_admitted_after_the_rest(clean_cal_keys, cal_namespace):
    seed = _governor(clean_cal_keys, cal_namespace)
    await seed.suspend("bot_challenge", 1)
    await asyncio.sleep(1.1)

    governors = [_governor(clean_cal_keys, cal_namespace) for _ in range(6)]
    outcomes = await _admissions(governors, "passe-1")

    assert outcomes.count("admitted") == 1
    assert outcomes.count("probe_in_flight") == 5


@pytest.mark.asyncio
async def test_a_concurrent_restriction_is_not_erased_by_a_collection_ending(
    clean_cal_keys, cal_namespace,
):
    governor = _governor(clean_cal_keys, cal_namespace)
    other = _governor(clean_cal_keys, cal_namespace)

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
async def test_an_abandoned_lease_expires_and_stops_blocking(clean_cal_keys, cal_namespace):
    stuck = _governor(clean_cal_keys, cal_namespace, lease_ttl_seconds=1.0, max_wait_seconds=0.05)

    async def slowish(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slowish)):
        budget = CalCollectionBudget(stuck)
        await stuck._acquire_slot(budget)

        blocked = await _admissions(
            [_governor(clean_cal_keys, cal_namespace, max_wait_seconds=0.05)], "passe-1",
        )
        assert blocked == ["admitted"]

        with pytest.raises(CalSuspendedError):
            await _governor(
                clean_cal_keys, cal_namespace, max_wait_seconds=0.05,
            )._acquire_slot(CalCollectionBudget(_governor(clean_cal_keys, cal_namespace)))

        await asyncio.sleep(1.1)
        await stuck._acquire_slot(CalCollectionBudget(stuck))
        assert await clean_cal_keys.zcard(stuck.lease_key) == 1


@pytest.mark.asyncio
async def test_an_unavailable_store_blocks_every_instance(
    clean_cal_keys, cal_namespace,  # noqa: ARG001
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
async def test_the_admission_wait_is_bounded_and_then_refused(clean_cal_keys, cal_namespace):
    holder = _governor(
        clean_cal_keys, cal_namespace, max_concurrent=1, min_interval_seconds=0.0,
        max_wait_seconds=0.3,
    )
    waiter = _governor(
        clean_cal_keys, cal_namespace, max_concurrent=1, min_interval_seconds=0.0,
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
async def test_a_user_trigger_outside_any_pass_shares_the_same_budget(
    clean_cal_keys, cal_namespace,
):
    scheduled = _governor(clean_cal_keys, cal_namespace)
    greeted = _governor(clean_cal_keys, cal_namespace)

    await scheduled.admit_book("passe-2026-10-21T12:15")
    await scheduled.admit_book("passe-2026-10-21T12:15")
    with pytest.raises(CalSuspendedError) as denied:
        await greeted.admit_book(None)

    assert denied.value.reason == "pass_budget"


@pytest.mark.asyncio
async def test_concurrent_collections_and_maintenance_share_one_serialised_lane(
    clean_cal_keys, cal_namespace,
):
    peak = {"active": 0, "max": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        peak["active"] += 1
        peak["max"] = max(peak["max"], peak["active"])
        await asyncio.sleep(0.02)
        peak["active"] -= 1
        return httpx.Response(200)

    collection = _governor(
        clean_cal_keys, cal_namespace, max_wait_seconds=10.0, min_interval_seconds=0.0,
    )
    maintenance = _governor(
        clean_cal_keys, cal_namespace, max_wait_seconds=10.0, min_interval_seconds=0.0,
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
async def test_a_successful_probe_never_erases_a_newer_restriction(clean_cal_keys, cal_namespace):
    clock = _SharedClock()
    governor = _governor(clean_cal_keys, cal_namespace, clock=clock, probe_ttl_seconds=600.0)
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
async def test_an_older_collection_cannot_conclude_someone_elses_probe(
    clean_cal_keys, cal_namespace,
):
    clock = _SharedClock()
    governor = _governor(clean_cal_keys, cal_namespace, clock=clock, probe_ttl_seconds=600.0)
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
    clean_cal_keys, cal_namespace,
):
    clock = _SharedClock()
    governor = _governor(clean_cal_keys, cal_namespace, clock=clock, probe_ttl_seconds=600.0)
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
async def test_an_interrupted_probe_does_not_close_the_circuit(clean_cal_keys, cal_namespace):
    clock = _SharedClock()
    governor = _governor(clean_cal_keys, cal_namespace, clock=clock)
    await governor.suspend("bot_challenge", 1)
    clock.advance(1.1)

    token = await governor.admit_book("passe-1")
    assert await governor.conclude_probe(token, succeeded=False, reason="pass_budget") != "closed"
    assert governor.phase() == "open"


@pytest.mark.asyncio
async def test_a_probe_consumes_an_admission_slot(clean_cal_keys, cal_namespace):
    clock = _SharedClock()
    governor = _governor(clean_cal_keys, cal_namespace, clock=clock, max_books_per_pass=2)
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
    clean_cal_keys, cal_namespace,
):
    governor = _governor(clean_cal_keys, cal_namespace, max_books_per_pass=2)

    await governor.admit_book("maintenance", counts_as_book=False)
    await governor.admit_book(None, counts_as_book=False)

    await governor.admit_book("passe-1")
    await governor.admit_book("passe-1")
    with pytest.raises(CalSuspendedError) as denied:
        await governor.admit_book("passe-1")
    assert denied.value.reason == "pass_budget"


@pytest.mark.asyncio
async def test_a_script_consumes_no_slot_from_the_books_pass(clean_cal_keys, cal_namespace):
    governor = _governor(clean_cal_keys, cal_namespace, max_books_per_pass=2)

    await governor.admit_book("script-chasse-prices", counts_as_book=False)
    await governor.admit_book("script-diagnostic", counts_as_book=False)

    assert await governor.admit_book("passe-1") is None
    assert await governor.admit_book("passe-1") is None


@pytest.mark.asyncio
async def test_a_failed_conclusion_never_shortens_a_longer_active_restriction(
    clean_cal_keys, cal_namespace,
):
    clock = _SharedClock()
    governor = _governor(clean_cal_keys, cal_namespace, clock=clock)
    await governor.suspend("bot_challenge", 1)
    clock.advance(1.1)

    token = await governor.admit_book("passe-1")
    assert token is not None
    await governor.suspend("bot_challenge", CAL_CHALLENGE_REST_SECONDS)
    before = (await governor.snapshot()).open_until

    outcome = await governor.conclude_probe(token, succeeded=False, reason="collection_aborted")

    assert outcome == "kept"
    assert (await governor.snapshot()).open_until == before
    assert (await governor.snapshot()).open_until - clock.now >= CAL_CHALLENGE_REST_SECONDS - 1


@pytest.mark.asyncio
async def test_a_deferral_conclusion_never_shortens_a_challenge(clean_cal_keys, cal_namespace):
    clock = _SharedClock()
    governor = _governor(clean_cal_keys, cal_namespace, clock=clock)
    await governor.suspend("bot_challenge", 1)
    clock.advance(1.1)

    token = await governor.admit_book("passe-1")
    assert token is not None
    await governor.suspend("bot_challenge", CAL_CHALLENGE_REST_SECONDS)
    before = (await governor.snapshot()).open_until

    assert await governor.conclude_probe(
        token, succeeded=False, reason="pass_budget",
    ) == "kept"
    assert (await governor.snapshot()).open_until == before


@pytest.mark.asyncio
async def test_a_longer_retry_after_survives_a_failed_conclusion(clean_cal_keys, cal_namespace):
    clock = _SharedClock()
    governor = _governor(clean_cal_keys, cal_namespace, clock=clock)
    await governor.suspend("bot_challenge", 1)
    clock.advance(1.1)

    token = await governor.admit_book("passe-1")
    assert token is not None
    await governor.suspend("rate_limited", 4 * 3600)
    before = (await governor.snapshot()).open_until

    await governor.conclude_probe(token, succeeded=False, reason="collection_aborted")

    assert (await governor.snapshot()).open_until == before
    assert (await governor.snapshot()).reason == "rate_limited"


@pytest.mark.asyncio
async def test_a_shorter_default_rest_still_applies_without_a_newer_restriction(
    clean_cal_keys, cal_namespace,
):
    clock = _SharedClock()
    governor = _governor(clean_cal_keys, cal_namespace, clock=clock)
    await governor.suspend("bot_challenge", 1)
    clock.advance(1.1)

    token = await governor.admit_book("passe-1")
    assert token is not None
    outcome = await governor.conclude_probe(token, succeeded=False, reason="collection_aborted")

    assert outcome == "reopened"
    assert (await governor.snapshot()).open_until - clock.now == CAL_PROBE_REST_SECONDS


@pytest.mark.asyncio
async def test_a_challenge_is_recorded_before_the_lease_is_released(
    clean_cal_keys, cal_namespace,
):
    clock = _SharedClock()
    governor = _governor(clean_cal_keys, cal_namespace, clock=clock, max_concurrent=1)
    observed_at_release: list[str] = []
    original_release = governor._release
    response_ready = asyncio.Event()
    release_response = asyncio.Event()
    calls: list[str] = []

    async def release_and_probe(token: str) -> None:
        observed_at_release.append(governor.phase())
        await original_release(token)

    governor._release = release_and_probe

    class Client:
        async def get(self, url: str, **_kwargs) -> httpx.Response:
            calls.append(url)
            if url.endswith("/challenge"):
                response_ready.set()
                await release_response.wait()
                return httpx.Response(200, text="<title>Vérification robot</title>")
            return httpx.Response(200, text="ok")

    started = asyncio.create_task(
        GovernedClient(Client(), CalCollectionBudget(governor)).get(
            "https://example.test/challenge",
        ),
    )
    await response_ready.wait()
    release_response.set()
    await started

    assert observed_at_release == ["open"]
    assert governor.phase() == "open"


@pytest.mark.asyncio
async def test_the_lease_is_released_even_when_the_call_raises(clean_cal_keys, cal_namespace):
    clock = _SharedClock()
    governor = _governor(clean_cal_keys, cal_namespace, clock=clock, max_concurrent=1)

    class Broken:
        async def get(self, _url: str, **_kwargs) -> httpx.Response:
            raise RuntimeError("transport")

    budget = CalCollectionBudget(governor)
    with pytest.raises(RuntimeError):
        await GovernedClient(Broken(), budget).get("https://example.test/x")

    assert await clean_cal_keys.zcard(governor.lease_key) == 0


@pytest.mark.asyncio
async def test_the_lease_is_released_when_the_call_is_cancelled(clean_cal_keys, cal_namespace):
    clock = _SharedClock()
    governor = _governor(clean_cal_keys, cal_namespace, clock=clock, max_concurrent=1)
    entered = asyncio.Event()

    class Slow:
        async def get(self, _url: str, **_kwargs) -> httpx.Response:
            entered.set()
            await asyncio.sleep(10)
            return httpx.Response(200, text="ok")

    task = asyncio.create_task(
        GovernedClient(Slow(), CalCollectionBudget(governor)).get("https://example.test/x"),
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await clean_cal_keys.zcard(governor.lease_key) == 0



async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_an_abandoned_probe_admits_one_new_probe_under_concurrency(
    clean_cal_keys, cal_namespace,
):
    clock = _SharedClock()
    governor = _governor(clean_cal_keys, cal_namespace, clock=clock, probe_ttl_seconds=60.0)
    await governor.suspend("bot_challenge", 1)
    clock.advance(1.1)

    abandoned = await governor.admit_book("passe-1")
    assert abandoned is not None
    clock.advance(61)
    assert governor.phase() == "open"

    outcomes = await asyncio.gather(*(
        _admit_outcome(governor, "passe-2") for _ in range(5)
    ))

    admitted = [token for token in outcomes if isinstance(token, str)]
    refused = [outcome for outcome in outcomes if not isinstance(outcome, str)]
    assert len(admitted) == 1
    assert len(refused) == 4
    assert {outcome.reason for outcome in refused} == {"probe_in_flight"}
    assert governor.phase() == "probing"

    assert await governor.conclude_probe(abandoned, succeeded=True) == "ignored"
    assert await governor.conclude_probe(admitted[0], succeeded=True) == "closed"


async def _admit_outcome(governor, occurrence: str):
    try:
        return await governor.admit_book(occurrence)
    except CalSuspendedError as exc:
        return exc


class _Emitter:
    """Émissions réelles via `GovernedClient`, sur un transport simulé compté."""

    def __init__(self, governor: CalGovernor, probe_token: str | None = None) -> None:
        self.governor = governor
        self.budget = CalCollectionBudget(governor)
        self.budget.probe_token = probe_token
        self.reached: list[str] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.reached.append(request.url.path)
        return httpx.Response(200, text="ok")

    async def get(self, path: str) -> str:
        transport = httpx.MockTransport(self.handler)
        async with httpx.AsyncClient(transport=transport) as client:
            try:
                await GovernedClient(client, self.budget).get(BASE + path)
            except CalSuspendedError as exc:
                return exc.reason
            return "emitted"


@pytest.mark.asyncio
async def test_a_collection_admitted_before_the_rest_cannot_emit_during_the_probe(
    clean_cal_keys, cal_namespace,
):
    clock = _SharedClock()
    governor = _governor(
        clean_cal_keys, cal_namespace, clock=clock, probe_ttl_seconds=60.0,
    )
    await governor.admit_book("passe-A")
    await governor.suspend("bot_challenge", 600)
    clock.advance(601)

    probe = await governor.admit_book("passe-B")
    assert probe is not None

    earlier = _Emitter(governor)
    earlier.budget.used = 1
    assert await earlier.get("/search") == "probe_in_flight"
    assert earlier.reached == []

    holder = _Emitter(governor, probe)
    assert await holder.get("/search") == "emitted"
    assert holder.reached == ["/search"]


@pytest.mark.asyncio
async def test_a_late_request_of_an_expired_probe_is_refused(clean_cal_keys, cal_namespace):
    clock = _SharedClock()
    governor = _governor(
        clean_cal_keys, cal_namespace, clock=clock, probe_ttl_seconds=60.0,
    )
    await governor.suspend("bot_challenge", 600)
    clock.advance(601)

    first = await governor.admit_book("passe-A")
    clock.advance(61)
    second = await governor.admit_book("passe-B")
    assert second is not None
    assert second != first

    late = _Emitter(governor, first)
    assert await late.get("/search") == "probe_in_flight"
    assert late.reached == []

    holder = _Emitter(governor, second)
    assert await holder.get("/search") == "emitted"
    assert holder.reached == ["/search"]


@pytest.mark.asyncio
async def test_the_probe_holder_emits_within_its_own_budgets(clean_cal_keys, cal_namespace):
    clock = _SharedClock()
    governor = _governor(
        clean_cal_keys, cal_namespace, clock=clock, probe_ttl_seconds=60.0,
    )
    await governor.suspend("bot_challenge", 600)
    clock.advance(601)
    probe = await governor.admit_book("passe-B")
    assert probe is not None

    holder = _Emitter(governor, probe)
    outcomes = [await holder.get(f"/search?page={page}") for page in range(5)]

    assert outcomes == ["emitted"] * 5
    assert len(holder.reached) == 5
    assert holder.budget.used == 5
    assert governor.emitted == 5


@pytest.mark.asyncio
async def test_a_valid_probe_conclusion_restores_normal_admission(clean_cal_keys, cal_namespace):
    clock = _SharedClock()
    governor = _governor(
        clean_cal_keys, cal_namespace, clock=clock, probe_ttl_seconds=60.0,
    )
    await governor.suspend("bot_challenge", 600)
    clock.advance(601)
    probe = await governor.admit_book("passe-B")
    assert probe is not None

    holder = _Emitter(governor, probe)
    assert await holder.get("/search") == "emitted"

    assert await governor.conclude_probe(probe, succeeded=True) == "closed"
    assert governor.phase() == "closed"

    other = _Emitter(governor)
    assert await other.get("/search") == "emitted"
    assert other.budget.probe_token is None
    assert other.reached == ["/search"]


@pytest.mark.asyncio
async def test_a_new_restriction_also_stops_the_probe_holder(clean_cal_keys, cal_namespace):
    clock = _SharedClock()
    governor = _governor(
        clean_cal_keys, cal_namespace, clock=clock, probe_ttl_seconds=60.0,
    )
    await governor.suspend("bot_challenge", 600)
    clock.advance(601)
    probe = await governor.admit_book("passe-B")
    assert probe is not None

    holder = _Emitter(governor, probe)
    assert await holder.get("/search") == "emitted"

    await governor.suspend("rate_limited", 900)
    assert await holder.get("/search?page=2") == "rate_limited"
    assert holder.reached == ["/search"]


@pytest.mark.asyncio
async def test_a_collection_resuming_after_the_rest_must_hold_the_probe(
    clean_cal_keys, cal_namespace,
):
    clock = _SharedClock()
    governor = _governor(
        clean_cal_keys, cal_namespace, clock=clock, probe_ttl_seconds=60.0,
    )
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request.url.path)
        return httpx.Response(200, text="ok")

    await governor.suspend("bot_challenge", 600)
    clock.advance(601)
    probe = await governor.admit_book("passe-B")
    assert probe is not None

    resuming = _Emitter(governor)
    resuming.budget.used = 3
    assert await resuming.get("/search") == "probe_in_flight"
    assert resuming.reached == []

    holder = _Emitter(governor, probe)
    assert await holder.get("/search") == "emitted"
    assert holder.reached == ["/search"]
