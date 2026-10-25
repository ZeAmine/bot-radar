from unittest.mock import AsyncMock, patch

import pytest

from app.bot.handlers.alerts_helpers import (
    extract_inline_price_and_clean_query,
    realign_draft_to_isbn,
)
from app.domain.rules.keyword_hygiene import sanitize_exclusions
from app.services.book_metadata_service import (
    BookMetadata,
    IsbnVerdict,
    parse_query_title_and_author,
)
from app.use_cases.book_search import result_from_isbn_verdict
from tests.fakes_market_source import source_history_values

ISBN = "9782505082545"


@pytest.mark.parametrize(
    ("query", "title", "author"),
    [
        ("Le jour de sable", "Le jour de sable", None),
        ("Le jour de sable De Jongh", "Le jour de sable", "De Jongh"),
        ("Jours de sable de Aimée de Jongh", "Jours de sable", "Aimée de Jongh"),
    ],
)
def test_title_and_author_particles(query, title, author):
    assert parse_query_title_and_author(query) == (title, author)


@pytest.mark.parametrize("query", ["9782505082545, 10€", "Jours de sable, 10€"])
def test_price_separator_is_not_part_of_query(query):
    title, price = extract_inline_price_and_clean_query(query)
    assert price == 1000
    assert not title.endswith(",")


def test_merged_exclusions_cannot_reject_confirmed_identity():
    terms = ["aimée de jongh", "dargaud", "carnet", "DARGAUD"]
    assert sanitize_exclusions(
        terms,
        "Jours de sable",
        author="Aimée de Jongh",
        publisher="Dargaud Benelux",
    ) == ["carnet"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Livre ISBN 9782505082545", "Le jour de sable"])
async def test_unidentified_draft_adopts_isbn_identity(name):
    notice = BookMetadata(
        title="Jours de sable",
        author="Aimée de Jongh",
        publisher="Dargaud Benelux",
        isbn13=ISBN,
        source="bnf",
    )
    result = result_from_isbn_verdict(IsbnVerdict(outcome="confirme", notice=notice))
    lookup = AsyncMock(return_value=result)
    with patch("app.bot.handlers.alerts_helpers.BookSearchUseCase") as factory:
        factory.return_value.search_by_isbn = lookup
        factory.return_value.search_by_title = AsyncMock(return_value=result)
        outcome = await realign_draft_to_isbn({"name": name, "book_unidentified": True}, ISBN)
    assert outcome.conflict is None
    assert outcome.updates["name"] == "Jours de sable"
    assert outcome.updates["include_keywords"] == ["jours de sable"]
    assert lookup.call_args.kwargs["expected_title"] is None


@pytest.mark.asyncio
async def test_provider_http_failure_retains_status_and_empty_success():
    import httpx

    from app.providers.momox import MomoxProvider
    from app.providers.registry import ProviderRegistry

    responses = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json={"data": []}),
    ]
    transport = httpx.MockTransport(lambda _request: responses.pop(0))
    async with httpx.AsyncClient(transport=transport) as client:
        registry = ProviderRegistry(providers=[MomoxProvider(http_client=client)])
        failed = await registry.fetch_all_offers(isbn13=ISBN, title="Jours de sable")
        empty = await registry.fetch_all_offers(isbn13=ISBN, title="Jours de sable")
    assert failed.attempts[0].reason == "rate_limited"
    assert failed.attempts[0].http_status == 429
    assert failed.attempts[0].retry_after_seconds == 7
    assert empty.attempts[0].status == "success"
    assert not empty.failed_providers


@pytest.mark.asyncio
async def test_transient_retry_does_not_repeat_successful_sources():
    from types import SimpleNamespace

    import httpx

    from app.db.models.book import Book
    from app.providers.momox import MomoxProvider
    from app.providers.registry import ProviderRegistry
    from app.services.book_market_pricing import fetch_offers_for_book

    healthy = SimpleNamespace(name="healthy", get_offers=AsyncMock(return_value=[]))
    calls = []

    def respond(request):
        calls.append(request.url.host)
        return httpx.Response(503 if len(calls) == 1 else 200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        offers = await fetch_offers_for_book(
            Book(title="Jours de sable", isbn13=ISBN),
            registry=ProviderRegistry(providers=[MomoxProvider(http_client=client), healthy]),
            retry_transient=True,
        )
    healthy.get_offers.assert_awaited_once()
    assert len(calls) == 2
    assert not offers.failed_providers
    assert offers.attempts[-1].status == "success"


@pytest.mark.asyncio
async def test_skip_preserves_one_shot_and_returns_recap():
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot.handlers.alerts_create_confirm import process_creation_edit_input
    from tests.add_fakes import DraftState
    from tests.screens import incoming_message

    state = DraftState()
    state.storage = MemoryStorage()
    state.key = StorageKey(bot_id=1, user_id=42, chat_id=42)
    state.data.update(
        name="Jours de sable",
        isbn=ISBN,
        is_series=False,
        category_slug="bd_comics",
        current_edit_field="isbn",
        pending_identity={"isbn": ISBN},
    )
    message = incoming_message("/skip")
    with patch("app.bot.handlers.alerts_create_confirm.relaunch_keyword_search"):
        await process_creation_edit_input(message, state)
    assert state.data["isbn"] is None
    assert not state.data["is_series"]
    assert state.data["pending_identity"] is None
    assert "Jours de sable" in message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_search_token_cannot_be_reused_after_cancel():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot.handlers.alerts_helpers import _begin_bibliographic_search, _search_still_current

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    old = await _begin_bibliographic_search(state)
    await state.clear()
    await _begin_bibliographic_search(state)
    assert not await _search_still_current(state, old)


@pytest.mark.asyncio
async def test_creation_reuses_a_valid_cache_without_changing_observation(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.db.models.book import Book
    from app.db.models.book_market_cache import BookMarketCache
    from app.domain.enums import ComparisonStatus
    from app.services import book_market_pricing
    from tests.database import sqlite_only_database

    observed = datetime.now(UTC) - timedelta(hours=1)
    async with sqlite_only_database() as database:
        book = Book(title="Jours de sable", normalized_title="jours de sable", isbn13=ISBN)
        database.session.add(book)
        await database.session.flush()
        database.session.add(
            BookMarketCache(
                book_id=book.id,
                collection_status="complete",
                checked_at=observed,
                last_successful_collection_at=observed,
                expires_at=observed + timedelta(hours=24),
                comparison_status=ComparisonStatus.AVAILABLE,
                lowest_delivered_price_cents=2059,
                **source_history_values(1900, observed),
            )
        )
        await database.session.commit()
        monkeypatch.setattr(book_market_pricing, "AsyncSessionLocal", database.maker)
        registry = AsyncMock()
        cache = await book_market_pricing.price_book_now(
            book.id, reason="alert_created", registry=registry
        )
        assert cache.lowest_delivered_price_cents == 2059
        assert cache.checked_at.replace(tzinfo=UTC) == observed
        registry.fetch_all_offers.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_conflict_does_not_change_identity_before_confirmation():
    from unittest.mock import MagicMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot.handlers.alerts_create_confirm import (
        confirm_creation_identity,
        process_creation_edit_input,
    )
    from tests.screens import incoming_message

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    await state.update_data(
        name="Autre œuvre",
        isbn="9782369903086",
        category_slug="bd_comics",
        current_edit_field="isbn",
        creation_token="creation",
        draft_revision=0,
    )
    notice = BookMetadata(title="Jours de sable", isbn13=ISBN, author="Aimée de Jongh")
    conflict = result_from_isbn_verdict(
        IsbnVerdict(outcome="confirme", notice=notice), expected_title="Autre œuvre"
    )
    confirmed = result_from_isbn_verdict(IsbnVerdict(outcome="confirme", notice=notice))
    with (
        patch("app.bot.handlers.alerts_helpers.BookSearchUseCase") as factory,
        patch(
            "app.bot.handlers.alerts_create_confirm.relaunch_keyword_search",
        ),
    ):
        factory.return_value.search_by_isbn = AsyncMock(side_effect=[conflict, confirmed])
        factory.return_value.search_by_title = AsyncMock(return_value=confirmed)
        message = incoming_message(ISBN)
        await process_creation_edit_input(message, state)
        assert (await state.get_data())["name"] == "Autre œuvre"
        keyboard = message.answer.call_args.kwargs["reply_markup"]
        callback = MagicMock()
        callback.answer = AsyncMock()
        callback.data = keyboard.inline_keyboard[0][0].callback_data.split("@")[0]
        callback.message = message
        await confirm_creation_identity(callback, state)
    draft = await state.get_data()
    assert draft["name"] == "Jours de sable"
    assert draft["isbn"] == ISBN
    assert draft["include_keywords"] == ["jours de sable"]


def test_creation_card_keeps_price_but_discloses_failed_collection():
    from datetime import UTC, datetime, timedelta

    from app.db.models.book import Book
    from app.db.models.book_market_cache import BookMarketCache
    from app.domain.enums import ComparisonStatus
    from app.services.market_price_service import MarketPriceService

    now = datetime.now(UTC)
    book = Book(title="Jours de sable")
    book.market_cache = BookMarketCache(
        lowest_delivered_price_cents=2059,
        comparison_status=ComparisonStatus.AVAILABLE,
        checked_at=now - timedelta(hours=2),
        expires_at=now + timedelta(hours=22),
        collection_status="failed",
        last_attempt_at=now,
    )
    text = MarketPriceService.get_comparison_summary(None, book, for_creation=True)
    assert "20,59 €" in text
    assert "Dernière offre connue" in text
    assert "collecte échouée" not in text
    assert "Prix bas constaté :</b> Non renseigné" in text


@pytest.mark.asyncio
async def test_missing_provider_schema_is_a_failure_not_an_empty_search():
    import httpx

    from app.providers.momox import MomoxProvider
    from app.providers.registry import ProviderRegistry

    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"unexpected": []}))
    async with httpx.AsyncClient(transport=transport) as client:
        registry = ProviderRegistry(providers=[MomoxProvider(http_client=client)])
        result = await registry.fetch_all_offers(
            isbn13=ISBN,
            title="Jours de sable",
        )
    assert result.failed_providers == ("momox",)
    assert result.attempts[0].reason == "invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "Le jour de sable, 10€",
        "Le jour de sable De Jongh, 10€",
        "9782505082545",
        "9782505082545, 10€",
        "Jours de sable de Aimée de Jongh, 10€",
    ],
)
@pytest.mark.parametrize("ai_enabled", [True, False])
async def test_add_sand_days_paths_share_the_canonical_identity(query, ai_enabled, monkeypatch):
    from unittest.mock import MagicMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot.handlers.alerts_create import handle_category_selected, start_add_alert
    from app.core.config import settings
    from app.services.book_metadata_service import BookResolution
    from tests.add_fakes import fake_bnf, fake_llm
    from tests.add_journey import drain, patched, repositories
    from tests.screens import message_with_recap

    notice = BookMetadata(
        title="Jours de sable",
        author="Aimée de Jongh",
        isbn13=ISBN,
        publisher="Dargaud Benelux",
        source="bnf",
    )
    bnf = fake_bnf(BookResolution(best=notice), verified_isbn=ISBN)
    client = fake_llm(
        {
            "name": "Le jour de sable",
            "category_slug": "bd_comics",
            "max_price_cents": 1000 if "10" in query else None,
            "include_keywords": ["le jour de sable"],
            "exclude_keywords": ["aimée de jongh", "dargaud"],
        }
    )
    repos = repositories(ISBN)
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, user_id=42, chat_id=42))
    message, _loading = message_with_recap("/add " + query)
    monkeypatch.setattr(settings, "AI_FEATURE_NATURAL_ADD", ai_enabled)
    patches = patched(repos, bnf, client, natural_enabled=ai_enabled)
    for item in patches:
        item.start()
    try:
        await start_add_alert(message, state)
        if not ai_enabled:
            client.generate_json.assert_not_awaited()
            if not query.startswith(ISBN):
                callback = MagicMock()
                callback.data = f"add_cat:bd_comics@{(await state.get_data())['creation_token']}"
                callback.answer = AsyncMock()
                callback.message = message
                await handle_category_selected(callback, state, AsyncMock(), None)
        await drain(repos["tasks"])
        draft = await state.get_data()
        assert draft["name"] == "Jours de sable"
        assert draft["isbn"] == ISBN
        assert draft["include_keywords"] == ["jours de sable"]
        assert "aimée de jongh" not in draft["exclude_keywords"]
        assert "dargaud" not in draft["exclude_keywords"]
        if "10€" in query:
            assert draft["max_item_price_cents"] == 1000
        if query.startswith(ISBN):
            client.generate_json.assert_not_awaited()
    finally:
        for item in reversed(patches):
            item.stop()


def test_typo_does_not_let_lexical_rank_choose_between_two_works():
    from app.services.book_metadata_service import BookMetadataService

    first = BookMetadata(title="Jours de sable", author="Aimée de Jongh", isbn13=ISBN)
    other = BookMetadata(title="Jour de sable", author="Autre auteur", isbn13="9782369903086")
    assert BookMetadataService._is_homonym(other, first, "Le jour de sable")


@pytest.mark.asyncio
async def test_same_title_different_author_is_a_real_identity_conflict():
    notice = BookMetadata(title="Jours de sable", author="Autre auteur", isbn13=ISBN)
    confirmed = result_from_isbn_verdict(IsbnVerdict(outcome="confirme", notice=notice))
    with patch("app.bot.handlers.alerts_helpers.BookSearchUseCase") as factory:
        factory.return_value.search_by_isbn = AsyncMock(return_value=confirmed)
        factory.return_value.search_by_title = AsyncMock(return_value=confirmed)
        result = await realign_draft_to_isbn(
            {"name": "Jours de sable", "author": "Aimée de Jongh", "isbn": "9782369903086"},
            ISBN,
        )
    assert result.conflict
    assert not result.updates


def test_cleaned_exclusions_allow_a_listing_naming_author_and_publisher():
    from app.db.models.alert import Alert
    from app.db.models.category import Category
    from app.domain.enums import AlertStatus
    from app.domain.rules.listing_match import evaluate_listing_against_alert
    from app.utils.text import normalize_text

    alert = Alert(
        name="Jours de sable",
        status=AlertStatus.ACTIVE,
        include_keywords=["jours de sable"],
        variants=[],
        exclude_keywords=sanitize_exclusions(
            ["aimée de jongh", "dargaud"],
            "Jours de sable",
            author="Aimée de Jongh",
            publisher="Dargaud Benelux",
        ),
        category=Category(slug="bd_comics", label="BD", emoji="B"),
    )
    result = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title=normalize_text("Jours de sable Aimée de Jongh Dargaud"),
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=1000,
    )
    assert result.is_match


def test_empty_current_collection_success_is_independent_of_history_failure():
    from app.db.models.book_market_cache import BookMarketCache
    from app.providers.base import CollectionAttempt, MarketOffers
    from app.services.book_market_pricing import BookPricingResult, _stamp_collection

    offers = MarketOffers(
        failed_providers=("source",),
        attempts=(
            CollectionAttempt("source"),
            CollectionAttempt("source", "history", "failed", "timeout"),
        ),
    )
    result = BookPricingResult.from_offers(offers, None, None)
    cache = BookMarketCache()
    _stamp_collection(cache, result)
    assert cache.last_successful_collection_at == result.collected_at
    assert cache.collection_status == "partial"
    assert cache.lowest_delivered_price_cents is None


def test_sources_not_queried_do_not_establish_collection_success():
    from app.providers.base import CollectionAttempt, MarketOffers
    from app.services.book_market_pricing import BookPricingResult

    result = BookPricingResult.from_offers(
        MarketOffers(attempts=(CollectionAttempt("source", status="skipped"),)),
        None,
        None,
    )
    assert not result.current_collection_succeeded


@pytest.mark.asyncio
async def test_cancelling_during_loading_message_cannot_resurrect_draft():
    import asyncio
    from unittest.mock import MagicMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import Message

    from app.bot.handlers.alerts_helpers import process_bd_query_lookup

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=42, user_id=42))
    await state.update_data(creation_token="old")
    entered, release = asyncio.Event(), asyncio.Event()
    status = MagicMock()
    status.edit_text = AsyncMock()

    async def loading(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return status

    message = MagicMock(spec=Message)
    message.answer = AsyncMock(side_effect=loading)
    found = result_from_isbn_verdict(
        IsbnVerdict(
            outcome="confirme",
            notice=BookMetadata(title="Jours de sable", isbn13=ISBN),
        )
    )
    with (
        patch("app.bot.handlers.alerts_helpers.BookSearchUseCase") as factory,
        patch(
            "app.bot.handlers.alerts_helpers._get_category_repo",
        ) as categories,
    ):
        factory.return_value.search_by_isbn = AsyncMock(return_value=found)
        categories.return_value.get_by_slug = AsyncMock(return_value=None)
        task = asyncio.create_task(process_bd_query_lookup(message, state, ISBN, AsyncMock()))
        await entered.wait()
        await state.clear()
        release.set()
        await task
    assert await state.get_data() == {}
    status.edit_text.assert_not_awaited()


def test_empty_collection_is_shown_once_and_missing_prices_are_not_retained():
    from datetime import UTC, datetime

    from app.db.models.book import Book
    from app.db.models.book_market_cache import BookMarketCache
    from app.services.market_price_service import MarketPriceService

    book = Book(title="Jours de sable")
    book.market_cache = BookMarketCache(
        collection_status="empty",
        checked_at=datetime.now(UTC),
        last_successful_collection_at=datetime.now(UTC),
    )
    text = MarketPriceService.get_comparison_summary(None, book, for_creation=True)
    assert text.count("Aucune offre d'occasion trouvée") == 1
    book.market_cache.collection_status = "failed"
    text = MarketPriceService.get_comparison_summary(None, book, for_creation=True)
    assert "prix conservés" not in text


def test_history_failure_does_not_turn_a_retained_price_into_a_current_offer():
    from datetime import UTC, datetime, timedelta

    from app.db.models.book import Book
    from app.db.models.book_market_cache import BookMarketCache
    from app.domain.enums import ComparisonStatus
    from app.services.market_price_service import MarketPriceService

    now = datetime.now(UTC)
    book = Book(title="Jours de sable")
    book.market_cache = BookMarketCache(
        collection_status="partial",
        current_offers=[],
        lowest_delivered_price_cents=2059,
        comparison_status=ComparisonStatus.AVAILABLE,
        checked_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=23),
        last_successful_collection_at=now,
        last_attempt_at=now,
        failed_provider_names=["source"],
    )
    text = MarketPriceService.get_comparison_summary(None, book, for_creation=True)
    assert "Dernière offre connue" in text
    assert "Aucune offre d'occasion trouvée" in text
    panel = MarketPriceService.format_market_details_card(None, book)
    assert "Meilleure offre actuelle" not in panel
