from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import app.providers.chasse_aux_livres as cal
from app.providers.base import requested_components
from app.providers.cal_governor import CalCollectionBudget, CalGovernor, UnregulatedGovernor
from app.services.book_market_pricing import BookPricingResult

OBSERVED_GROUP_COUNTS = (13, 20, 20, 21, 21, 28, 28, 32)
OBSERVED_RESPONSE_SECONDS = (.071, .323, .560, .834, 1.086, 1.341, 1.583, 1.838)


class LookupClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.now = round(self.now + delay, 6)


async def replay_synthetic_groups(
    monkeypatch, counts, *, response_seconds=None, response_cost=.05,
    regulated=False, error=None, oversleep=0.0, prior_requests=0,
):
    clock = LookupClock()
    monkeypatch.setattr(cal, 'time', SimpleNamespace(monotonic=clock.monotonic))
    requests = []
    received_counts = []
    governor = CalGovernor(clock=clock.monotonic) if regulated else UnregulatedGovernor()
    if regulated:
        monkeypatch.setattr(governor, '_run', AsyncMock(return_value=['admitted']))
    budget = CalCollectionBudget(governor)
    offer = {
        'logo': {'name': 'SYNTHETIC'}, 'condition': {'_name': 'USED'},
        'subCondition': {'name': 'Très bon'}, 'price': {'amount': 1200},
        'shippingCost': {'amount': 250}, 'totalPrice': {'amount': 1450},
    }

    def respond(request):
        requests.append(request)
        if request.url.path == '/search':
            return httpx.Response(200, text='synthetic')
        if request.url.path.startswith('/prix/'):
            return httpx.Response(200, text=(
                '<div data-asin="2756079332" data-lvs="synthetic" data-nbeng="32"></div>'
            ))
        index = int(request.url.params['retry'])
        if response_seconds is None:
            clock.now = round(clock.now + response_cost, 6)
        else:
            clock.now = max(clock.now, response_seconds[index])
        if error is not None and index == 1:
            if isinstance(error, Exception):
                raise error
            return error
        count = counts[min(index, len(counts) - 1)]
        previous = counts[min(index - 1, len(counts) - 1)] if index else 0
        received_counts.append(count)
        return httpx.Response(200, json={'offers': {
            str(i): [offer] if i == 2 else [] for i in range(previous, count)
        }})

    async def sleep(delay):
        await clock.sleep(delay)
        clock.now += oversleep

    token = requested_components.set(frozenset({'current'}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            for _ in range(prior_requests):
                await budget.get(client, cal.ChasseAuxLivresProvider.BASE_URL + '/search')
            result = await cal.ChasseAuxLivresProvider(
                http_client=client, sleep=sleep, collection_budget=budget,
            ).get_offers(
                isbn13='9782756079332', title='Ouvrage',
                source_url=cal.ChasseAuxLivresProvider.BASE_URL + '/prix/2756079332/9782756079332',
            )
    finally:
        requested_components.reset(token)
    return result, requests, clock, budget, received_counts


@pytest.mark.asyncio
async def test_observed_progression_reaches_all_groups_with_new_defaults(monkeypatch):
    result, requests, clock, budget, counts = await replay_synthetic_groups(
        monkeypatch, OBSERVED_GROUP_COUNTS, response_seconds=OBSERVED_RESPONSE_SECONDS,
        regulated=True,
    )
    assert counts == list(OBSERVED_GROUP_COUNTS)
    assert cal.LOOKUP_POLL_MAX == 12
    assert cal.LOOKUP_POLL_BUDGET_SECONDS == 5.0
    assert cal.LOOKUP_POLL_DELAY_SECONDS == .2
    assert result.attempts[0].status == 'success'
    assert result.attempts[0].engines_received == 32
    assert result.attempts[0].poll_count == 7
    assert result.attempts[0].lookup_seconds == 1.838
    assert len(requests) == budget.used == 9
    assert clock.sleeps == [.2] * 7
    assert result[0].delivered_price_cents == 1450
    assert [r.url.params.get('retry') for r in requests] == [None, *map(str, range(8))]
    for index, request in enumerate(requests[1:]):
        previous = OBSERVED_GROUP_COUNTS[index - 1] if index else 0
        assert request.url.params['engines'].split('_') == list(map(str, range(previous, 32)))


@pytest.mark.asyncio
async def test_old_duration_stops_observed_progression_at_28_groups(monkeypatch):
    monkeypatch.setattr(cal, 'LOOKUP_POLL_MAX', 12)
    monkeypatch.setattr(cal, 'LOOKUP_POLL_BUDGET_SECONDS', 1.5)
    result, requests, clock, budget, counts = await replay_synthetic_groups(
        monkeypatch, OBSERVED_GROUP_COUNTS, response_seconds=OBSERVED_RESPONSE_SECONDS,
        regulated=True,
    )
    assert counts == list(OBSERVED_GROUP_COUNTS[:6])
    assert result.attempts[0].status == 'partial'
    assert result.attempts[0].engines_received == 28
    assert result.attempts[0].poll_count == 5
    assert len(requests) == budget.used == 7
    assert clock.now + .2 >= 1.5
    pricing = BookPricingResult.from_offers(result, result[0], None)
    assert pricing.price_confirmed is False
    assert pricing.current_collection_complete is False
    assert pricing.lowest_delivered_price_cents is None


@pytest.mark.asyncio
@pytest.mark.parametrize(('counts', 'polls'), [((32,), 0), ((13, 32), 1), ((13, 20, 32), 2)])
async def test_complete_groups_stop_without_using_remaining_budget(monkeypatch, counts, polls):
    result, requests, clock, budget, received = await replay_synthetic_groups(monkeypatch, counts)
    assert result.attempts[0].status == 'success'
    assert result.attempts[0].poll_count == polls
    assert len(requests) == budget.used == polls + 2
    assert clock.sleeps == [.2] * polls
    assert received == list(counts)


@pytest.mark.asyncio
async def test_duration_bound_stops_before_another_wait(monkeypatch):
    result, requests, clock, budget, _ = await replay_synthetic_groups(
        monkeypatch, (13,), response_cost=.3,
    )
    assert result.attempts[0].status == 'partial'
    assert result.attempts[0].poll_count == 9
    assert len(requests) == budget.used == 11
    assert clock.now == pytest.approx(4.8)
    assert clock.sleeps == [.2] * 9
    assert clock.now + .2 >= cal.LOOKUP_POLL_BUDGET_SECONDS


@pytest.mark.asyncio
async def test_expired_budget_after_sleep_does_not_emit_another_request(monkeypatch):
    result, requests, clock, budget, _ = await replay_synthetic_groups(
        monkeypatch, (13,), oversleep=5,
    )
    assert result.attempts[0].status == 'partial'
    assert result.attempts[0].poll_count == 0
    assert len(requests) == budget.used == 2
    assert clock.sleeps == [.2]


@pytest.mark.asyncio
async def test_poll_bound_is_independent_of_time_and_global_budget(monkeypatch):
    monkeypatch.setattr(cal, 'LOOKUP_POLL_MAX', 2)
    result, requests, clock, budget, _ = await replay_synthetic_groups(monkeypatch, (13,))
    assert result.attempts[0].status == 'partial'
    assert result.attempts[0].poll_count == 2
    assert len(requests) == budget.used == 4
    assert clock.now < 5
    assert clock.sleeps == [.2] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize('prior_requests', [0, 2])
async def test_global_request_bound_counts_product_and_prevents_thirteenth_get(
    monkeypatch, prior_requests,
):
    result, requests, clock, budget, _ = await replay_synthetic_groups(
        monkeypatch, (13,), regulated=True, prior_requests=prior_requests,
    )
    assert budget.governor.max_requests_per_collection == 12
    assert budget.used == budget.governor.emitted == len(requests) == 12
    assert requests[prior_requests].url.path.startswith('/prix/')
    assert requests[-1].url.params['retry'] == str(10 - prior_requests)
    assert budget.suspension.reason == 'collection_budget'
    assert budget.governor.denied == 1
    assert clock.now < 5
    assert result.attempts[0].status == 'partial'
    assert result.attempts[0].reason == 'incomplete_offers'
    assert not BookPricingResult.from_offers(result, result[0], None).price_confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize(('error', 'status', 'reason'), [
    (httpx.Response(429), 'failed', 'rate_limited'),
    (httpx.Response(200, headers={'cf-mitigated': 'challenge'}), 'failed', 'bot_challenge'),
    (httpx.Response(500), 'partial', 'incomplete_offers'),
    (httpx.Response(200, content=b''), 'partial', 'incomplete_offers'),
    (httpx.Response(200, json={'offers': []}), 'partial', 'incomplete_offers'),
    (httpx.ReadTimeout('synthetic'), 'partial', 'incomplete_offers'),
])
async def test_poll_failure_preserves_stop_and_certification(monkeypatch, error, status, reason):
    result, requests, clock, budget, _ = await replay_synthetic_groups(
        monkeypatch, (13,), error=error,
    )
    assert len(requests) == budget.used == 3
    assert clock.sleeps == [.2]
    assert result.attempts[0].status == status
    assert result.attempts[0].reason == reason
    assert result.failed_providers == ('chasse_aux_livres',)
    pricing = BookPricingResult.from_offers(result, result[0], None)
    assert pricing.price_confirmed is False
    assert pricing.current_collection_complete is False
