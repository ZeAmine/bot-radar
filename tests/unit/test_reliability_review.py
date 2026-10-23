import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import httpx
import pytest

from tests.add_fakes import DraftState
from tests.screens import incoming_message


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["start", "lookup"])
async def test_duplicate_lookup_cannot_rewrite_a_replaced_draft(entry):
    from app.bot.handlers.alerts_create import start_add_alert
    from app.bot.handlers.alerts_helpers import process_bd_query_lookup

    state = DraftState()
    state.data = {"creation_token": "old"}
    entered, release = asyncio.Event(), asyncio.Event()

    async def find(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(
            id="existing", name="Ancienne œuvre", isbn=None, category=None,
            max_item_price_cents=1000, status="active", preparation=None,
        )

    repo = MagicMock()
    repo.find_existing = AsyncMock(side_effect=find)
    message = incoming_message("/add Ancienne œuvre")
    with (
        patch("app.bot.handlers.alerts_create._get_alert_repo", return_value=repo),
        patch("app.bot.handlers.alerts_helpers._get_alert_repo", return_value=repo),
    ):
        operation = (
            start_add_alert(message, state, MagicMock(), SimpleNamespace(id="user"))
            if entry == "start" else process_bd_query_lookup(
                message, state, "Ancienne œuvre", MagicMock(), SimpleNamespace(id="user")
            )
        )
        task = asyncio.create_task(operation)
        await entered.wait()
        await state.clear()
        await state.update_data(creation_token="new", name="Nouvelle œuvre")
        release.set()
        await task
    assert state.data == {"creation_token": "new", "name": "Nouvelle œuvre"}
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_silence_after_cancel_does_not_restore_category_state():
    from app.bot.handlers.alerts_create import start_add_alert

    state = DraftState()
    entered, release = asyncio.Event(), asyncio.Event()

    async def parse(_query):
        entered.set()
        await release.wait()
        return None

    with patch("app.bot.handlers.alerts_create.natural_add_module") as module:
        module.is_enabled = True
        module.parse_prompt = AsyncMock(side_effect=parse)
        task = asyncio.create_task(start_add_alert(incoming_message("/add Livre"), state))
        await entered.wait()
        await state.clear()
        release.set()
        await task
    assert state.data == {}
    assert state.state is None


def test_history_only_partial_result_does_not_claim_successful_empty_search():
    from datetime import UTC, datetime, timedelta

    from app.db.models.book import Book
    from app.db.models.book_market_cache import BookMarketCache
    from app.services.market_price_service import MarketPriceService

    now = datetime.now(UTC)
    book = Book(title="Jours de sable")
    book.market_cache = BookMarketCache(
        collection_status="partial", current_offers=[],
        last_attempt_at=now, last_successful_collection_at=now - timedelta(days=1),
    )
    text = MarketPriceService.get_comparison_summary(None, book, for_creation=True)
    assert "Aucune offre d'occasion trouvée" not in text
    assert "partielle" not in text


@pytest.mark.asyncio
async def test_cancelled_provider_keeps_its_failed_attempt_alongside_success():
    from app.providers.registry import ProviderRegistry

    failed = SimpleNamespace(
        name="cancelled", get_offers=AsyncMock(side_effect=asyncio.CancelledError),
    )
    healthy = SimpleNamespace(name="healthy", get_offers=AsyncMock(return_value=[]))
    offers = await ProviderRegistry(providers=[failed, healthy]).fetch_all_offers(
        isbn13=None, title="Jours de sable",
    )
    assert offers.failed_providers == ("cancelled",)
    assert {(a.provider, a.status) for a in offers.attempts} == {
        ("cancelled", "failed"), ("healthy", "success"),
    }


@pytest.mark.asyncio
async def test_chasse_missing_search_payload_is_a_failure():
    from app.providers.chasse_aux_livres import ChasseAuxLivresProvider

    def respond(request):
        if request.url.path == "/search":
            return httpx.Response(200, text='<div data-hash="abcdefghijklmnop"></div>')
        return httpx.Response(200, json={"unexpected": "schema"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        offers = await ChasseAuxLivresProvider(http_client=client, max_retries=0)._search_query(
            "Jours de sable",
        )
    assert offers.failed_providers == ("chasse_aux_livres",)
    assert offers.attempts[0].reason == "invalid_response"


@pytest.mark.asyncio
async def test_cancelled_delivery_releases_occurrence_without_acknowledging_incident(monkeypatch):
    from app.providers.base import CollectionAttempt
    from app.services import market_health
    from tests.unit.test_refresh_notifications import result

    client = AsyncMock()
    client.get.return_value = None
    client.set.return_value = True
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    send = AsyncMock(side_effect=[asyncio.CancelledError, True])
    monkeypatch.setattr(market_health, "notify_admins", send)
    report = result(attempts=[CollectionAttempt("source", status="failed", reason="timeout")])
    with pytest.raises(asyncio.CancelledError):
        await market_health.notify_refresh_result(report)
    assert not market_health._incident_states
    await market_health.notify_refresh_result(report)
    assert send.await_count == 2
    assert "Nouvel incident" in send.call_args.args[0]


@pytest.mark.asyncio
async def test_reused_cache_is_reported_without_a_successful_collection(monkeypatch):
    from app.db.models.book_market_cache import BookMarketCache
    from app.services import market_health
    from app.tasks.refresh_book_prices import _book_result, _run_wave
    from tests.unit.test_refresh_notifications import result

    outcome = _book_result(BookMarketCache(collection_status="complete"), None)
    stats = dict.fromkeys(["processed", "refreshed", "available", "unavailable",
                          "degraded", "errors", "empty", "reused"], 0)
    monkeypatch.setattr("app.tasks.refresh_book_prices._price_one_book",
                        AsyncMock(return_value=outcome))
    await _run_wave([SimpleNamespace(id="book")], MagicMock(), None, asyncio.Semaphore(1), stats)
    assert stats["reused"] == 1
    assert stats["refreshed"] == 0
    client = AsyncMock()
    client.get.return_value = None
    client.set.return_value = True
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(market_health, "notify_admins", send)
    report = result()
    report.stats = stats
    await market_health.notify_refresh_result(report)
    assert "Caches réutilisés : 1" in send.call_args.args[0]
    assert "Actualisées : 0" in send.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["background", "button"])
async def test_keyword_result_renders_the_current_conditions(mode):
    from app.bot.handlers.alerts_create import (
        _search_keywords_in_background,
        handle_create_ai_variants,
    )
    from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome

    state = DraftState()
    state.data = {
        "creation_token": "creation", "draft_revision": 0,
        "name": "Jours de sable", "category_slug": "bd_comics",
        "include_keywords": ["jours de sable"], "selected_conditions": ["Très bon état"],
    }
    await state.set_state("AlertCreationState:confirm_creation")
    await state.update_data(keyword_analysis_status="failed")
    snapshot = await state.get_data()
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return KeywordVariantsOutcome(variants=[], exclusions=[], answered=True)

    loading = MagicMock()
    loading.edit_text = AsyncMock()
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = loading
    tasks = []
    def schedule(coro, **_kwargs):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    with (
        patch("app.bot.handlers.alerts_create.keyword_variants_module.generate_variants_and_exclusions",
              new=generate),
        patch("app.services.ai.modules.keyword_variants.KeywordVariantsModule.is_enabled",
              new_callable=PropertyMock, return_value=True),
        patch("app.bot.handlers.alerts_create.keyword_variants_module.client",
              MagicMock(is_configured=True)),
        patch("app.bot.handlers.alerts_create.safe_create_task", schedule),
    ):
        operation = (
            _search_keywords_in_background(
                loading_msg=loading, event=incoming_message("/add"), state=state,
                snapshot=snapshot, name="Jours de sable", category_slug="bd_comics",
                book_context=None,
            ) if mode == "background" else handle_create_ai_variants(callback, state)
        )
        task = asyncio.create_task(operation)
        await asyncio.wait_for(entered.wait(), 1)
        await state.update_data(selected_conditions=["Bon état"])
        release.set()
        await task
        await asyncio.gather(*tasks)
    assert state.data["selected_conditions"] == ["Bon état"]
    text = loading.edit_text.call_args.args[0]
    assert ":</b> BE" in text
    assert ":</b> TBE" not in text


def test_partial_history_failure_does_not_claim_the_current_source_is_down():
    from datetime import UTC, datetime

    from app.db.models.book import Book
    from app.db.models.book_market_cache import BookMarketCache
    from app.domain.enums import ComparisonStatus
    from app.services.market_price_service import MarketPriceService

    now = datetime.now(UTC)
    book = Book(title="Jours de sable")
    book.market_cache = BookMarketCache(
        collection_status="partial", lowest_delivered_price_cents=2059,
        current_offers=[{"provider_name": "source", "delivered_price_cents": 2059}],
        comparison_status=ComparisonStatus.AVAILABLE, checked_at=now,
        last_successful_collection_at=now, last_attempt_at=now,
        failed_provider_names=["source"],
    )
    text = MarketPriceService.get_comparison_summary(None, book, for_creation=True)
    assert "20,59 €" in text
    assert "Collecte partielle" not in text
    assert "certaines données n’ont pas été actualisées" not in text
    assert "sources sont indisponibles" not in text


@pytest.mark.parametrize("known_price", [None, 2059])
def test_skipped_sources_never_become_an_empty_success_or_a_current_offer(known_price):
    from datetime import UTC, datetime, timedelta

    from app.db.models.book import Book
    from app.db.models.book_market_cache import BookMarketCache
    from app.domain.enums import ComparisonStatus
    from app.providers.base import CollectionAttempt, MarketOffers
    from app.services.book_market_pricing import BookPricingResult, _stamp_collection
    from app.services.market_price_service import MarketPriceService

    now = datetime.now(UTC)
    old = now - timedelta(hours=1)
    result = BookPricingResult.from_offers(
        MarketOffers(attempts=(CollectionAttempt("source", status="skipped"),)), None, None,
    )
    book = Book(title="Jours de sable")
    cache = BookMarketCache(
        lowest_delivered_price_cents=known_price, current_offers=[],
        collection_status="complete", checked_at=old, expires_at=now + timedelta(hours=1),
        last_successful_collection_at=old,
        comparison_status=(ComparisonStatus.AVAILABLE if known_price
                           else ComparisonStatus.UNAVAILABLE),
    )
    book.market_cache = cache
    _stamp_collection(cache, result)
    assert cache.collection_status == "skipped"
    assert cache.last_successful_collection_at == old
    assert not cache.failed_provider_names
    assert not result.current_collection_succeeded
    text = MarketPriceService.get_comparison_summary(None, book, for_creation=True)
    assert "Aucune source interrogée" not in text
    assert "Aucune offre d'occasion trouvée" not in text
    assert "collecte échouée" not in text
    if known_price:
        assert "Dernière offre connue" in text
        assert "20,59 €" in text
        panel = MarketPriceService.format_market_details_card(None, book)
        assert "Meilleure offre actuelle" not in panel
