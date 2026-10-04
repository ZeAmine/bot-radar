"""Tests unitaires pour ProcessListingUseCase et AsyncEnrichListingUseCase."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.constants import ComparisonStatus
from app.domain.entities.analysis import ListingAnalysisResult, RealConditionAssessment
from app.domain.entities.listing import ListingEntity
from app.domain.enums import CategorySlug
from app.domain.protocols.llm import LLMAnalysisProviderProtocol
from app.domain.rules.matcher import DeterministicListingMatcher
from app.schemas.listing import ListingIngestPayload
from app.use_cases.enrich_listing_ai import AsyncEnrichListingUseCase
from app.use_cases.ingest_listing import ProcessListingUseCase


@pytest.fixture
def mock_db() -> AsyncMock:
    """Session SQLAlchemy mockée."""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


@pytest.fixture
def mock_bot() -> AsyncMock:
    """Bot Telegram mocké."""
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_async_enrich_listing_with_risk_warning(mock_bot: AsyncMock) -> None:
    """Vérifie qu'un avertissement Telegram est envoyé si le risque est élevé."""
    mock_llm = AsyncMock(spec=LLMAnalysisProviderProtocol)
    mock_llm.analyze_listing.return_value = ListingAnalysisResult(
        real_condition=RealConditionAssessment.POOR_OR_DEFECTIVE,
        is_lot_incomplete=False,
        is_scam_or_trap=True,
        risk_score=85,
        analysis_summary="Boîte vide vendue sans console.",
    )

    use_case = AsyncEnrichListingUseCase(llm_provider=mock_llm, bot=mock_bot)
    listing = ListingEntity(
        id=uuid.uuid4(),
        source_name="vinted",
        external_id="12345",
        title="PS5 Boîte Seule",
        normalized_title="ps5 boite seule",
        category_slug=CategorySlug.GAMING,
        item_price_cents=5000,
        condition="Bon état",
        description="Juste le carton d'origine",
    )

    result = await use_case.execute(chat_id=123456, message_id=1, listing=listing)

    assert result is not None
    assert result.is_scam_or_trap is True
    mock_bot.send_message.assert_awaited_once()
    _, kwargs = mock_bot.send_message.call_args
    assert "Alerte suspicion" in kwargs["text"]
    assert "Boîte vide" in kwargs["text"]


@pytest.mark.asyncio
async def test_async_enrich_listing_no_warning_when_safe(mock_bot: AsyncMock) -> None:
    """Vérifie qu'aucun message n'est envoyé si l'article est sain."""
    mock_llm = AsyncMock(spec=LLMAnalysisProviderProtocol)
    mock_llm.analyze_listing.return_value = ListingAnalysisResult(
        real_condition=RealConditionAssessment.VERY_GOOD,
        is_lot_incomplete=False,
        is_scam_or_trap=False,
        risk_score=10,
        analysis_summary="Article en parfait état.",
    )

    use_case = AsyncEnrichListingUseCase(llm_provider=mock_llm, bot=mock_bot)
    listing = ListingEntity(
        id=uuid.uuid4(),
        source_name="vinted",
        external_id="12345",
        title="One Piece Tome 1",
        normalized_title="one piece tome 1",
        category_slug=CategorySlug.BD_COMICS,
        item_price_cents=400,
        condition="Très bon état",
        description="Lu une fois",
    )

    result = await use_case.execute(chat_id=123456, message_id=1, listing=listing)

    assert result is not None
    assert result.risk_score == 10
    mock_bot.send_message.assert_not_called()


def test_comparison_status_has_no_out_of_stock() -> None:
    """Le cache hors stock utilise UNAVAILABLE, pas un membre absent."""
    assert ComparisonStatus.UNAVAILABLE.value == "unavailable"
    assert not hasattr(ComparisonStatus, "OUT_OF_STOCK")


@pytest.mark.asyncio
async def test_process_listing_duplicate(mock_db: AsyncMock) -> None:
    """Vérifie qu'un doublon est détecté immédiatement sans notifier."""
    use_case = ProcessListingUseCase(db=mock_db)

    # Simuler une annonce existante
    existing_mock = MagicMock()
    existing_mock.id = uuid.uuid4()
    existing_mock.estimated_total_cents = 1200
    existing_mock.category_slug = "tech"
    existing_mock.normalized_title = "titre"
    existing_mock.description = ""
    existing_mock.condition = "Bon état"
    existing_mock.item_price_cents = 1000
    existing_mock.isbn13 = None
    existing_mock.url = "https://vinted.fr/item-dup"

    mock_repo = AsyncMock()
    mock_repo.get_by_source_and_external_id.return_value = existing_mock
    mock_repo.match_notification_state.return_value = (set(), set())
    mock_repo.get_active_alerts_by_category.return_value = []
    use_case.listing_repo = mock_repo

    payload = ListingIngestPayload(
        source_name="vinted",
        external_id="item-dup",
        url="https://vinted.fr/item-dup",
        title="Titre",
        category_slug="tech",
        item_price_cents=1000,
    )

    response = await use_case.execute(payload, raw_payload={})

    assert response.is_duplicate is True
    assert response.listing_id == existing_mock.id
    assert len(response.matches) == 0


def test_deterministic_matcher_adapter() -> None:
    """Vérifie que l'adaptateur DeterministicListingMatcher s'exécute correctement."""
    from app.core.constants import AlertStatus

    matcher = DeterministicListingMatcher()
    listing = ListingEntity(
        id=uuid.uuid4(),
        source_name="vinted",
        external_id="item-test",
        title="Livre Tintin au Tibet",
        normalized_title="livre tintin au tibet",
        category_slug=CategorySlug.BD_COMICS,
        item_price_cents=800,
    )

    # Mock alerte
    alert_mock = MagicMock()
    alert_mock.id = uuid.uuid4()
    alert_mock.name = "Tintin"
    alert_mock.status = AlertStatus.ACTIVE
    alert_mock.category = MagicMock(slug="bd_comics")
    alert_mock.include_keywords = ["tintin"]
    alert_mock.exclude_keywords = []
    alert_mock.variants = []
    alert_mock.max_item_price_cents = 1500
    alert_mock.accepted_conditions = None
    alert_mock.isbn = None

    match_res = matcher.evaluate(listing, alert_mock)
    assert match_res.is_match is True
    assert match_res.price_matches is True


@pytest.mark.asyncio
async def test_process_listing_bd_resolves_local_market_comparison(mock_db: AsyncMock) -> None:
    """La ligne de cote locale est transmise à l'envoi Telegram."""
    mock_send_alert = AsyncMock(return_value=True)

    market_line = "📉 Prix bas constaté (T6) : 5,12 €\n⛔️ Écart : <b>+13,16 €</b>"
    with (
        patch("app.api.routes.ingest.send_listing_alert", mock_send_alert),
        patch(
            "app.use_cases.ingest_listing.comparison_line_from_loaded_books",
            return_value=(market_line, True, None),
        ),
        patch(
            "app.db.repositories.listing_repo.ListingRepository.mark_notified",
            new_callable=AsyncMock,
        ),
        patch("app.db.session.AsyncSessionLocal") as session_local,
    ):
        session_local.return_value.__aenter__.return_value = AsyncMock()
        session_local.return_value.__aexit__.return_value = None
        use_case = ProcessListingUseCase(db=mock_db)

        # Simuler le repo listing
        mock_listing = MagicMock()
        mock_listing.id = uuid.uuid4()
        mock_listing.source_name = "vinted"
        mock_listing.external_id = "test-123"
        mock_listing.title = "Blacksad - Tome 6"
        mock_listing.normalized_title = "blacksad tome 6"
        mock_listing.category_slug = "bd_comics"
        mock_listing.item_price_cents = 1400
        mock_listing.estimated_total_cents = 1828
        mock_listing.condition = "Très bon état"
        mock_listing.description = "Tome 6 en parfait état"
        mock_listing.url = "https://vinted.fr/item-123"
        mock_listing.photos = ["https://img.vinted.fr/1.jpg"]

        mock_repo = AsyncMock()
        mock_repo.get_by_source_and_external_id.return_value = None
        mock_repo.create_from_payload.return_value = mock_listing
        mock_repo.create_match.return_value = MagicMock()

        from app.core.constants import AlertStatus

        alert_mock = MagicMock()
        alert_mock.id = uuid.uuid4()
        alert_mock.name = "Blacksad"
        alert_mock.status = AlertStatus.ACTIVE
        alert_mock.category = MagicMock(slug="bd_comics")
        alert_mock.isbn = None
        alert_mock.include_keywords = ["blacksad"]
        alert_mock.exclude_keywords = []
        alert_mock.variants = []
        alert_mock.max_item_price_cents = 2000
        alert_mock.accepted_conditions = None
        alert_mock.user = MagicMock(telegram_user_id=123456)

        mock_repo.get_active_alerts_by_category.return_value = [alert_mock]
        use_case.listing_repo = mock_repo

        use_case.listing_repo.mark_notified = AsyncMock()

        payload = ListingIngestPayload(
            source_name="vinted",
            external_id="test-123",
            url="https://vinted.fr/item-123",
            title="Blacksad - Tome 6",
            category_slug="bd_comics",
            item_price_cents=1400,
        )

        resp = await use_case.execute(payload, raw_payload={})
        assert resp.is_duplicate is False
        assert len(resp.matches) == 1

        # Vérifier que send_alert a reçu la ligne de marché épurée
        mock_send_alert.assert_awaited_once()
        _, kwargs = mock_send_alert.call_args
        assert kwargs["market_comparison_line"] == (
            "📉 Prix bas constaté (T6) : 5,12 €\n⛔️ Écart : <b>+13,16 €</b>"
        )

        # L'enrichissement IA part dans send_listing_alert, pas dans un second use case.
        use_case.listing_repo.mark_notified.assert_awaited_once()


def _bd_scenario(market_cache: object) -> tuple[AsyncMock, AsyncMock, MagicMock, MagicMock]:
    """Annonce BD, session et dépôt simulés, avec un tome unique déjà lié à l'alerte.

    `market_cache` None reproduit l'alerte partie sans cote : le livre est identifié,
    mais aucun marchand n'a encore répondu pour lui.
    """
    from app.core.constants import AlertStatus

    mock_listing = MagicMock()
    mock_listing.id = uuid.uuid4()
    mock_listing.category_slug = "bd_comics"
    mock_listing.normalized_title = "blacksad tome 6"
    mock_listing.title = "Blacksad - Tome 6"
    mock_listing.description = "Tome 6 en parfait état"
    mock_listing.condition = "Très bon état"
    mock_listing.item_price_cents = 1400
    mock_listing.estimated_total_cents = 1828
    mock_listing.isbn13 = None
    mock_listing.url = "https://vinted.fr/item-123"
    mock_listing.photos = []

    book = MagicMock()
    book.id = uuid.uuid4()
    book.title = "Blacksad - Tome 6"
    book.isbn13 = "9782205078054"
    book.market_cache = market_cache

    alert_mock = MagicMock()
    alert_mock.id = uuid.uuid4()
    alert_mock.name = "Blacksad"
    alert_mock.status = AlertStatus.ACTIVE
    alert_mock.category = MagicMock(slug="bd_comics")
    alert_mock.isbn = None
    alert_mock.include_keywords = ["blacksad"]
    alert_mock.exclude_keywords = []
    alert_mock.variants = []
    alert_mock.max_item_price_cents = 2000
    alert_mock.accepted_conditions = None
    alert_mock.user = MagicMock(telegram_user_id=123456)
    alert_mock.alert_books = [MagicMock(book=book)]

    mock_repo = AsyncMock()
    mock_repo.get_by_source_and_external_id.return_value = None
    mock_repo.create_from_payload.return_value = mock_listing
    mock_repo.create_match.return_value = MagicMock()
    mock_repo.mark_notified = AsyncMock()
    mock_repo.get_active_alerts_by_category.return_value = [alert_mock]

    return mock_listing, mock_repo, alert_mock, book


@pytest.mark.asyncio
async def test_bd_alert_without_price_is_priced_in_background(mock_db: AsyncMock) -> None:
    """Un livre sans cote est annoncé puis coté, jamais avant l'envoi de l'alerte.

    L'ordre se lit dans les appels : l'alerte part d'abord, la cotation part après.
    Sans elle, un livre laissé vide le restait jusqu'au prochain rafraîchissement
    planifié — c'est ce qui faisait apparaître une alerte sans prix ni écart.
    """
    from app.services.market_price_service import PENDING_COTE_LINE

    calls: list[str] = []

    async def _send(**_kwargs: object) -> bool:
        calls.append("send")
        return True

    def _schedule(_book_id: object, *, reason: str) -> None:
        calls.append(f"price:{reason}")

    mock_send_alert = AsyncMock(side_effect=_send)
    with (
        patch("app.api.routes.ingest.send_listing_alert", mock_send_alert),
        patch(
            "app.db.repositories.listing_repo.ListingRepository.mark_notified",
            new_callable=AsyncMock,
        ),
        patch("app.db.session.AsyncSessionLocal"),
        patch(
            "app.use_cases.ingest_listing.schedule_book_pricing",
            side_effect=_schedule,
        ) as schedule,
    ):
        use_case = ProcessListingUseCase(db=mock_db)
        _mock_listing, mock_repo, _alert, book = _bd_scenario(None)
        use_case.listing_repo = mock_repo
        payload = ListingIngestPayload(
            source_name="vinted",
            external_id="test-123",
            url="https://vinted.fr/item-123",
            title="Blacksad - Tome 6",
            category_slug="bd_comics",
            item_price_cents=1400,
        )
        await use_case.execute(payload, raw_payload={})

    mock_send_alert.assert_awaited_once()
    _, kwargs = mock_send_alert.call_args
    assert kwargs["market_comparison_line"] == PENDING_COTE_LINE
    # L'invariant n°1 se lit dans l'ordre des appels : l'alerte d'abord, la cote ensuite.
    assert calls == ["send", "price:alert_sent"]
    schedule.assert_called_once_with(book.id, reason="alert_sent")


@pytest.mark.asyncio
async def test_bd_alert_with_price_is_not_priced_again(mock_db: AsyncMock) -> None:
    """Un livre déjà coté est comparé, et sa cote n'est pas redemandée."""
    from app.db.models.book_market_cache import BookMarketCache
    from app.services.market_price_service import PENDING_COTE_LINE

    cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=uuid.uuid4(),
        lowest_delivered_price_cents=1200,
        comparison_status=ComparisonStatus.AVAILABLE,
        historical_low_1y_cents=1000,
    )
    mock_send_alert = AsyncMock(return_value=True)

    with (
        patch("app.api.routes.ingest.send_listing_alert", mock_send_alert),
        patch(
            "app.db.repositories.listing_repo.ListingRepository.mark_notified",
            new_callable=AsyncMock,
        ),
        patch("app.db.session.AsyncSessionLocal"),
        patch("app.use_cases.ingest_listing.schedule_book_pricing") as schedule,
    ):
        use_case = ProcessListingUseCase(db=mock_db)
        _mock_listing, mock_repo, _alert, book = _bd_scenario(cache)
        use_case.listing_repo = mock_repo
        payload = ListingIngestPayload(
            source_name="vinted",
            external_id="test-123",
            url="https://vinted.fr/item-123",
            title="Blacksad - Tome 6",
            category_slug="bd_comics",
            item_price_cents=1400,
        )
        await use_case.execute(payload, raw_payload={})

    schedule.assert_not_called()
    _, kwargs = mock_send_alert.call_args
    line = kwargs["market_comparison_line"]
    assert "📉 Prix bas constaté (T6) : 10,00 €" in line
    assert "Écart" in line
    assert PENDING_COTE_LINE not in line
    assert book.market_cache is cache


@pytest.mark.asyncio
async def test_mark_notified_commits_before_the_next_send(mock_db: AsyncMock) -> None:
    """La première alerte est horodatée même si l'envoi suivant lève."""
    first = MagicMock()
    first.id = uuid.uuid4()
    first.name = "Blacksad"
    first.include_keywords = ["blacksad"]
    first.user = MagicMock(telegram_user_id=1)
    second = MagicMock()
    second.id = uuid.uuid4()
    second.name = "Blacksad 2"
    second.include_keywords = ["blacksad"]
    second.user = MagicMock(telegram_user_id=2)

    mock_listing = MagicMock()
    mock_listing.id = uuid.uuid4()
    mock_listing.category_slug = "tech"
    mock_listing.normalized_title = "blacksad"
    mock_listing.description = "tome"
    mock_listing.condition = "Bon état"
    mock_listing.item_price_cents = 1000
    mock_listing.isbn13 = None
    mock_listing.url = "https://vinted.fr/x"
    mock_listing.estimated_total_cents = 1200
    mock_listing.photos = ["https://img.example/a.jpg"]
    mock_listing.title = "Blacksad"

    mock_repo = AsyncMock()
    mock_repo.get_by_source_and_external_id.return_value = None
    mock_repo.create_from_payload.return_value = mock_listing
    mock_repo.get_active_alerts_by_category.return_value = [first, second]
    mock_repo.create_match.return_value = MagicMock()
    mock_repo.mark_notified = AsyncMock()

    send_calls = {"n": 0}

    async def _send(**_kwargs: object) -> bool:
        send_calls["n"] += 1
        if send_calls["n"] == 2:
            raise RuntimeError("telegram down")
        return True

    use_case = ProcessListingUseCase(db=mock_db)
    use_case.listing_repo = mock_repo
    payload = ListingIngestPayload(
        source_name="vinted",
        external_id="two-alerts",
        url="https://vinted.fr/x",
        title="Blacksad",
        category_slug="tech",
        item_price_cents=1000,
    )
    with (
        patch("app.api.routes.ingest.send_listing_alert", side_effect=_send),
        patch(
            "app.use_cases.ingest_listing.evaluate_listing_against_alert",
            return_value=MagicMock(
                is_match=True,
                keyword_match_score=1,
                condition_matches=True,
                price_matches=True,
            ),
        ),pytest.raises(RuntimeError, match="telegram down")
    ):
        await use_case.execute(payload, raw_payload={})

    mock_repo.mark_notified.assert_awaited_once_with([(first.id, mock_listing.id)])


@pytest.mark.asyncio
async def test_bd_resolution_schedules_bnf_without_awaiting_it(mock_db: AsyncMock) -> None:
    """La recherche BNF ne bloque pas la comparaison locale."""
    use_case = ProcessListingUseCase(db=mock_db)
    listing = MagicMock(title="Blacksad", isbn13=None)
    alert = MagicMock(id=uuid.uuid4(), isbn=None)

    def _close_task(coro, **_kwargs):
        coro.close()
        return MagicMock()

    with (
        patch("app.use_cases.ingest_listing.BookRepository") as repo_cls,
        patch("app.use_cases.ingest_listing.BookCatalogService"),
        patch(
            "app.use_cases.ingest_listing.MarketPriceService.get_comparison_summary",
            return_value=None,
        ),
        patch(
            "app.use_cases.ingest_listing.safe_create_task",
            side_effect=_close_task,
        ) as scheduled,
    ):
        repo_cls.return_value.resolve_book_for_listing = AsyncMock(
            return_value=(None, None, None)
        )
        result = await use_case._resolve_bd_market_comparison(listing, alert)

    assert result is None
    scheduled.assert_called_once()
    assert scheduled.call_args.kwargs["name"] == f"bnf_link_{alert.id}"


