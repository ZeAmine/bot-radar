import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.providers.cal_governor import (
    ACQUIRE_SCRIPT,
    RELEASE_SCRIPT,
    CalCollectionBudget,
    CalGovernor,
    CalSuspendedError,
)

pytest_plugins = ["tests.unit.cal_redis_fixture"]


async def shared_budget_race(store=None, namespace=""):
    started = 0
    both_started = asyncio.Event()
    first_released = asyncio.Event()
    armed = False
    acquisitions = 0
    calls = []
    governor = CalGovernor(store=store, namespace=namespace)
    original_run = governor._run

    async def delayed_run(script, *args, **kwargs):
        nonlocal acquisitions
        if armed and script == ACQUIRE_SCRIPT:
            acquisitions += 1
            if acquisitions == 1:
                await both_started.wait()
            else:
                await first_released.wait()
        response = (
            await original_run(script, *args, **kwargs) if store is not None else ["admitted"]
        )
        if armed and script == RELEASE_SCRIPT:
            first_released.set()
        return response

    governor._run = delayed_run
    budget = CalCollectionBudget(governor)

    async def send(client):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        return await budget.get(client, "https://example.invalid/synthetic")

    def handler(request):
        calls.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(11):
            await budget.get(client, "https://example.invalid/seed")
        armed = True
        results = await asyncio.wait_for(
            asyncio.gather(
                send(client),
                send(client),
                return_exceptions=True,
            ),
            5,
        )
    return budget, calls, results


async def test_shared_budget_reserves_last_place_before_redis_wait():
    budget, calls, results = await shared_budget_race()
    assert len(calls) == budget.used == 12
    assert sum(isinstance(r, httpx.Response) for r in results) == 1
    assert sum(isinstance(r, CalSuspendedError) for r in results) == 1


@pytest.mark.parametrize("limit", [1, 2, 12])
async def test_each_automatic_redirect_requires_a_budget_place(monkeypatch, limit):
    governor = CalGovernor()
    monkeypatch.setattr(governor, "_run", AsyncMock(return_value=["admitted"]))
    budget = CalCollectionBudget(governor, limit=limit)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": f"/hop/{len(calls)}"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(CalSuspendedError) as error:
            await budget.get(client, "https://example.invalid/start")
    assert error.value.reason == "collection_budget"
    assert len(calls) == budget.used == limit
    assert budget.suspension.reason == "collection_budget"


@pytest.mark.parametrize(("status", "method", "body"), [(303, "GET", b""), (307, "POST", b"value")])
async def test_redirects_preserve_httpx_method_body_and_history(monkeypatch, status, method, body):
    governor = CalGovernor()
    monkeypatch.setattr(governor, "_run", AsyncMock(return_value=["admitted"]))
    budget = CalCollectionBudget(governor)
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(status, headers={"location": "/done"})
            if len(calls) == 1
            else httpx.Response(200)
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        response = await budget.post(client, "https://example.invalid/start", content=b"value")
    assert len(calls) == budget.used == 2
    assert calls[1].method == method
    assert calls[1].content == body
    assert [r.status_code for r in response.history] == [status]
    assert response.status_code == 200


async def test_explicit_no_redirect_remains_one_emission(monkeypatch):
    governor = CalGovernor()
    monkeypatch.setattr(governor, "_run", AsyncMock(return_value=["admitted"]))
    budget = CalCollectionBudget(governor)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "/done"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        response = await budget.get(client, "https://example.invalid/start", follow_redirects=False)
    assert response.status_code == 302
    assert len(calls) == budget.used == 1


async def test_failed_emission_is_charged_and_reservation_is_released(monkeypatch):
    governor = CalGovernor()
    monkeypatch.setattr(governor, "_run", AsyncMock(return_value=["admitted"]))
    budget = CalCollectionBudget(governor, limit=1)
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout):
            await budget.get(client, "https://example.invalid/timeout")
        with pytest.raises(CalSuspendedError):
            await budget.get(client, "https://example.invalid/not-sent")
    assert len(calls) == budget.used == 1


async def test_cancelled_redis_wait_does_not_leave_a_budget_reservation(monkeypatch):
    governor = CalGovernor()
    entered = asyncio.Event()

    async def pending(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(governor, "_run", pending)
    budget = CalCollectionBudget(governor, limit=1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        task = asyncio.create_task(budget.get(client, "https://example.invalid/cancel"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert budget.used == 0
        monkeypatch.setattr(governor, "_run", AsyncMock(return_value=["admitted"]))
        assert (await budget.get(client, "https://example.invalid/allowed")).status_code == 200
    assert budget.used == 1


async def test_shared_budget_race_with_real_redis(clean_cal_keys, cal_namespace):
    budget, calls, results = await shared_budget_race(clean_cal_keys, cal_namespace)
    assert len(calls) == budget.used == 12
    assert sum(isinstance(r, CalSuspendedError) for r in results) == 1


@pytest.mark.parametrize(("seed", "parallelism"), [(0, 2), (0, 32), (0, 128), (11, 32), (11, 128)])
async def test_many_concurrent_emissions_never_exceed_shared_budget(
    clean_cal_keys,
    cal_namespace,
    seed,
    parallelism,
):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace)
    budget = CalCollectionBudget(governor)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(seed):
            await budget.get(client, "https://example.invalid/seed")
        results = await asyncio.gather(
            *(budget.get(client, "https://example.invalid/parallel") for _ in range(parallelism)),
            return_exceptions=True,
        )
    assert len(calls) == budget.used == governor.emitted == min(12, seed + parallelism)
    assert sum(isinstance(r, httpx.Response) for r in results) == len(calls) - seed
    assert all(isinstance(r, (httpx.Response, CalSuspendedError)) for r in results)


async def test_redirects_are_counted_by_the_real_redis_window(clean_cal_keys, cal_namespace):
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace)
    budget = CalCollectionBudget(governor)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": f"/hop/{len(calls)}"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(CalSuspendedError):
            await budget.get(client, "https://example.invalid/start")
    assert len(calls) == budget.used == governor.emitted == 12
    assert await clean_cal_keys.zcard(governor.window_key) == 12


async def test_cross_origin_redirect_does_not_restore_authorization(monkeypatch):
    governor = CalGovernor()
    monkeypatch.setattr(governor, "_run", AsyncMock(return_value=["admitted"]))
    budget = CalCollectionBudget(governor)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "https://other.invalid/done"})
        return httpx.Response(200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        response = await budget.get(
            client,
            "https://example.invalid/start",
            headers={"authorization": "synthetic"},
        )
    assert response.status_code == 200
    assert "authorization" not in calls[1].headers
    assert len(calls) == budget.used == 2


async def test_redirect_limit_still_stops_without_unbounded_following(monkeypatch):
    governor = CalGovernor()
    monkeypatch.setattr(governor, "_run", AsyncMock(return_value=["admitted"]))
    budget = CalCollectionBudget(governor)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "/loop"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        max_redirects=2,
    ) as client:
        with pytest.raises(httpx.TooManyRedirects):
            await budget.get(client, "https://example.invalid/start")
    assert len(calls) == budget.used == 3


async def test_many_collections_share_two_admissions_and_twenty_four_emissions(
    clean_cal_keys,
    cal_namespace,
):
    governors = [CalGovernor(store=clean_cal_keys, namespace=cal_namespace) for _ in range(32)]
    admissions = await asyncio.gather(
        *(g.admit_book("synthetic-pass") for g in governors), return_exceptions=True
    )
    accepted = [
        g
        for g, result in zip(governors, admissions, strict=True)
        if not isinstance(result, BaseException)
    ]
    assert len(accepted) == 2
    assert all(isinstance(r, CalSuspendedError) for r in admissions if isinstance(r, BaseException))
    budgets = [CalCollectionBudget(g) for g in accepted]
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await asyncio.gather(
            *(
                budget.get(client, f"https://example.invalid/book{index}")
                for index, budget in enumerate(budgets)
                for _ in range(64)
            ),
            return_exceptions=True,
        )
    assert len(calls) == sum(b.used for b in budgets) == 24
    assert [calls.count(f"/book{i}") for i in range(2)] == [12, 12]
    assert sum(isinstance(r, httpx.Response) for r in results) == 24
    assert await clean_cal_keys.zcard(accepted[0].admit_key) == 2
    assert await clean_cal_keys.zcard(accepted[0].window_key) == 24
