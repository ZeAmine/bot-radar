"""Preuve d'émission : distinguer un refus avant départ d'une interruption.

Le régulateur peut refuser une collecte avant qu'aucune requête ne parte, ou
après que la collecte a commencé. Seul le premier cas est un report
administratif : le second a réellement épuisé des requêtes et reste un échec
imputable à la collecte. La preuve est portée par le budget de collecte
jusqu'au résultat que lit la persistance.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import CollectionAttempt, MarketOffers
from app.providers.cal_governor import CalGovernor, CalSuspendedError
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
from app.services.administrative_deferral import administrative_deferral_only
from app.services.book_market_pricing import (
    BookPricingResult,
    persist_market_result,
)
from tests.unit.test_chasse_aux_livres_provider import (
    MOCK_LOOKUP_JSON,
    MOCK_PROD_HTML,
    MOCK_REST_JSON,
    MOCK_SEARCH_PAGE_HTML,
)

pytest_plugins = ["tests.unit.cal_redis_fixture"]

BASE = "https://www.chasse-aux-livres.fr"
TITLE = "Rip Tome 1"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def _governor(clock: _Clock, store, namespace: str, **kwargs) -> CalGovernor:
    async def advance(seconds: float) -> None:
        clock.now += seconds

    options = {
        "store": store, "clock": clock, "namespace": namespace, "sleep": advance,
    }
    options.update(kwargs)
    return CalGovernor(**options)


def _chain_handler(nbeng: str = "1") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/search":
            return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
        if path == "/rest/search-results":
            return httpx.Response(200, json=MOCK_REST_JSON)
        if path.startswith("/prix/"):
            return httpx.Response(200, text=MOCK_PROD_HTML.replace(
                'data-nbeng="1"', f'data-nbeng="{nbeng}"',
            ))
        return httpx.Response(200, json=MOCK_LOOKUP_JSON)

    return httpx.MockTransport(handler)


async def _collect(governor: CalGovernor, transport: httpx.MockTransport) -> MarketOffers:
    async with httpx.AsyncClient(transport=transport) as client:
        provider = ChasseAuxLivresProvider(
            http_client=client, max_retries=0, governor=governor,
        )
        offers = await provider.get_offers(isbn13=None, title=TITLE)
    return offers


async def _persist(cache: BookMarketCache, result: BookPricingResult, _now: datetime | None = None):
    repo = MagicMock()
    repo.save_snapshot = AsyncMock()
    repo.refresh_annual_references = AsyncMock()
    repo.get_cache_by_book_id = AsyncMock(return_value=cache)
    repo.update_or_create_cache = AsyncMock(return_value=cache)
    with patch(
        "app.services.book_market_pricing.MarketPriceRepository", return_value=repo,
    ), patch(
        "app.services.book_market_pricing._refresh_series_totals", AsyncMock(),
    ), patch(
        "app.services.book_market_pricing.AsyncSessionLocal", MagicMock(),
    ):
        return await persist_market_result(AsyncMock(), cache.book_id, result)


@pytest.fixture
def pass_token():
    from app.providers.cal_governor import pass_occurrence

    token = pass_occurrence.set("passe-emission")
    yield token
    pass_occurrence.reset(token)


@pytest.mark.usefixtures("pass_token")
@pytest.mark.asyncio
async def test_a_refusal_before_any_request_proves_zero_emission(clean_cal_keys, cal_namespace):
    clock = _Clock()
    governor = _governor(clock, clean_cal_keys, cal_namespace)
    await governor.suspend("bot_challenge", 3600)

    offers = await _collect(governor, _chain_handler())

    assert offers.emitted_requests == 0
    assert offers.attempts[0].reason == "bot_challenge"
    result = BookPricingResult.from_offers(offers, None, None)
    assert result.emitted_requests == 0
    assert administrative_deferral_only(result) is True

    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)
    written = await _persist(cache, result, datetime.now(UTC))

    assert written.collection_status == "deferred"
    assert written.last_attempt_at is None
    assert (written.refresh_state or {}).get("current") is None


@pytest.mark.usefixtures("pass_token")
@pytest.mark.asyncio
async def test_a_started_search_that_is_then_refused_is_not_zero_emission(
    clean_cal_keys, cal_namespace,
):
    clock = _Clock()
    governor = _governor(
        clock, clean_cal_keys, cal_namespace, max_requests_per_collection=1,
    )

    offers = await _collect(governor, _chain_handler())

    assert offers.emitted_requests == 1
    assert offers.attempts[0].reason == "collection_budget"
    result = BookPricingResult.from_offers(offers, None, None)
    assert result.emitted_requests == 1
    assert administrative_deferral_only(result) is False

    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)
    written = await _persist(cache, result, datetime.now(UTC))

    assert written.collection_status == "skipped"
    assert written.last_attempt_at is not None
    assert "administrative" not in (written.refresh_state or {})
    assert governor.denied >= 1


@pytest.mark.usefixtures("pass_token")
@pytest.mark.asyncio
async def test_offers_received_then_interrupted_keep_a_partial(
    clean_cal_keys, cal_namespace,
):
    clock = _Clock()
    governor = _governor(
        clock, clean_cal_keys, cal_namespace, max_requests_per_collection=4,
    )

    offers = await _collect(governor, _chain_handler(nbeng="2"))

    current = [a for a in offers.attempts if a.operation == "current"]
    assert current
    assert current[0].status == "partial"
    assert current[0].reason == "incomplete_offers"
    assert list(offers)
    assert offers.emitted_requests == 4

    result = BookPricingResult.from_offers(offers, None, None)
    assert administrative_deferral_only(result) is False

    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)
    written = await _persist(cache, result, datetime.now(UTC))

    assert written.collection_status == "partial"
    assert written.last_attempt_at is not None
    assert "administrative" not in (written.refresh_state or {})


@pytest.mark.usefixtures("pass_token")
@pytest.mark.asyncio
async def test_another_source_answering_keeps_the_mixed_behaviour(clean_cal_keys, cal_namespace):
    clock = _Clock()
    governor = _governor(clock, clean_cal_keys, cal_namespace)
    await governor.suspend("bot_challenge", 3600)

    cal = await _collect(governor, _chain_handler())
    assert cal.emitted_requests == 0

    other = MarketOffers(
        attempts=(CollectionAttempt("moment", "current", "success"),),
    )
    mixed = MarketOffers(
        list(cal), failed_providers=cal.failed_providers,
        attempts=tuple(cal.attempts) + tuple(other.attempts),
        emitted_requests=cal.emitted_requests + other.emitted_requests,
    )

    result = BookPricingResult.from_offers(mixed, None, None)
    assert administrative_deferral_only(result) is False

    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)
    written = await _persist(cache, result, datetime.now(UTC))

    assert written.collection_status == "empty"
    assert written.last_successful_collection_at is not None


@pytest.mark.usefixtures("pass_token")
@pytest.mark.asyncio
async def test_a_restriction_opening_mid_collection_is_not_zero_emission(
    clean_cal_keys, cal_namespace,
):
    """La raison est administrative, mais une requête réelle est déjà partie."""
    clock = _Clock()
    governor = _governor(clock, clean_cal_keys, cal_namespace)

    class OpeningClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, _url: str, **_kwargs) -> httpx.Response:
            self.calls += 1
            await governor.suspend("bot_challenge", 3600)
            return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)

    client = OpeningClient()
    provider = ChasseAuxLivresProvider(governor=governor)
    provider.http_client = client  # type: ignore[assignment]
    offers = await provider.get_offers(isbn13=None, title=TITLE)

    assert client.calls == 1
    assert offers.attempts[0].reason == "bot_challenge"
    assert offers.emitted_requests == 1

    result = BookPricingResult.from_offers(offers, None, None)
    assert administrative_deferral_only(result) is False

    cache = BookMarketCache(book_id=uuid.uuid4())
    cache.next_refresh_at = datetime.now(UTC)
    written = await _persist(cache, result, datetime.now(UTC))

    assert written.collection_status != "deferred"
    assert "administrative" not in (written.refresh_state or {})


@pytest.mark.usefixtures("pass_token")
@pytest.mark.asyncio
async def test_a_collection_interrupted_by_the_rest_is_readmitted_not_resumed(
    clean_cal_keys, cal_namespace,
):
    """Une collecte déjà admise ne reprend pas après rétablissement.

    Règle retenue : seul un appel frais de `get_offers`, avec sa propre
    admission, peut émettre. Une collecte dont le budget existe déjà mais dont le
    client a été démonté est refusée en `readmission_required` plutôt que de
    continuer sur l'admission précédente.
    """
    clock = _Clock()
    governor = _governor(clock, clean_cal_keys, cal_namespace)

    class OpeningClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, _url: str, **_kwargs) -> httpx.Response:
            self.calls += 1
            await governor.suspend("bot_challenge", 3600)
            return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)

    client = OpeningClient()
    provider = ChasseAuxLivresProvider(governor=governor)
    provider.http_client = client  # type: ignore[assignment]
    offers = await provider.get_offers(isbn13=None, title=TITLE)
    assert offers.emitted_requests == 1

    provider._cal_client_instance = None
    with pytest.raises(CalSuspendedError) as refused:
        provider._cal_client()

    assert refused.value.reason == "readmission_required"
    assert client.calls == 1
