"""Tests unitaires pour ProcessListingUseCase et AsyncEnrichListingUseCase."""

import uuid
from datetime import UTC, datetime
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

    market_line = "<b>Prix bas constaté (T6) :</b> 5,12 €\n<b>Écart :</b> +13,16 € ⛔️"
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
        mock_listing.isbn13 = None

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
            "<b>Prix bas constaté (T6) :</b> 5,12 €\n<b>Écart :</b> +13,16 € ⛔️"
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
    assert "Cote indisponible" in kwargs["market_comparison_line"]
    assert "à l’envoi" in kwargs["market_comparison_line"]
    # L'invariant n°1 se lit dans l'ordre des appels : l'alerte d'abord, la cote ensuite.
    assert calls == ["send", "price:alert_sent"]
    schedule.assert_called_once_with(book.id, reason="alert_sent")


@pytest.mark.asyncio
async def test_bd_alert_whose_pass_found_nothing_is_dated_instead_of_pending(
    mock_db: AsyncMock,
) -> None:
    """Un livre déjà coté — sans résultat — se dit par sa date, pas par une attente.

    L'alerte annonçait « cote en cours de calcul » à chaque annonce pour un livre
    dont toutes les passes étaient revenues vides : le seul mensonge disponible
    était d'attendre encore. La cotation reste relancée, elle : c'est une offre
    qui peut apparaître, pas la ligne qui doit mentir.
    """
    from app.db.models.book_market_cache import BookMarketCache
    from app.services.market_price_service import NO_OFFER_LINE, PENDING_COTE_LINE

    cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=uuid.uuid4(),
        lowest_delivered_price_cents=None,
        comparison_status=ComparisonStatus.UNAVAILABLE,
        historical_low_1y_cents=None,
        checked_at=datetime(2026, 10, 6, 9, 30, tzinfo=UTC),
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

    line = mock_send_alert.call_args.kwargs["market_comparison_line"]
    assert line.startswith(NO_OFFER_LINE)
    assert "06/10" in line
    assert PENDING_COTE_LINE not in line
    # L'absence d'offre n'interdit pas la passe suivante : le livre reste demandé.
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
    assert "<b>Prix bas constaté (T6) :</b> 10,00 €" in line
    assert "Écart" in line
    assert PENDING_COTE_LINE not in line
    assert book.market_cache is cache


@pytest.mark.asyncio
async def test_series_alert_prices_every_linked_volume_after_the_send(mock_db: AsyncMock) -> None:
    """E1 : une alerte série cote chaque volume lié sans prix, pas seulement le tome résolu.

    Sans cette garantie, un volume dont la cotation a échoué à la création restait
    muet jusqu'au job glissant — le prix bas de chaque volume n'était pas en base.
    """
    calls: list[str] = []

    async def _send(**_kwargs: object) -> bool:
        calls.append("send")
        return True

    def _schedule(book_id: object, *, reason: str) -> None:
        calls.append(f"price:{reason}:{book_id}")

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
        _mock_listing, mock_repo, alert, book = _bd_scenario(None)
        # Un second volume lié à la même alerte série, sans cote.
        sibling = MagicMock()
        sibling.id = uuid.uuid4()
        sibling.title = "Blacksad - Tome 5"
        sibling.market_cache = None
        alert.alert_books = [MagicMock(book=book), MagicMock(book=sibling)]
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

    # Les deux volumes sont cotés, après l'envoi.
    assert f"price:alert_sent:{book.id}" in calls
    assert f"price:alert_sent:{sibling.id}" in calls
    assert calls.index("send") < calls.index(f"price:alert_sent:{sibling.id}")
    assert schedule.call_count == 2


@pytest.mark.asyncio
async def test_series_alert_does_not_reprice_a_priced_sibling(mock_db: AsyncMock) -> None:
    """E1 : un volume déjà coté n'est pas recoté — la garantie ne doit pas doubler le travail."""
    from app.db.models.book_market_cache import BookMarketCache

    calls: list[str] = []

    async def _send(**_kwargs: object) -> bool:
        calls.append("send")
        return True

    def _schedule(book_id: object, *, reason: str) -> None:
        calls.append(f"price:{reason}:{book_id}")

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
        _mock_listing, mock_repo, alert, book = _bd_scenario(None)
        # Un second volume lié, déjà coté.
        sibling = MagicMock()
        sibling.id = uuid.uuid4()
        sibling.title = "Blacksad - Tome 5"
        sibling.market_cache = BookMarketCache(
            id=uuid.uuid4(),
            book_id=sibling.id,
            lowest_delivered_price_cents=800,
            comparison_status=ComparisonStatus.AVAILABLE,
            historical_low_1y_cents=750,
        )
        alert.alert_books = [MagicMock(book=book), MagicMock(book=sibling)]
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

    # Seul le tome résolu (sans cote) est coté.
    assert f"price:alert_sent:{book.id}" in calls
    assert f"price:alert_sent:{sibling.id}" not in calls
    assert schedule.call_count == 1


@pytest.mark.asyncio
async def test_isbn_alert_does_not_price_siblings(mock_db: AsyncMock) -> None:
    """E1 : une alerte ISBN ne déclenche pas la couverture de série — elle n'a pas de série."""
    calls: list[str] = []

    async def _send(**_kwargs: object) -> bool:
        calls.append("send")
        return True

    def _schedule(book_id: object, *, reason: str) -> None:
        calls.append(f"price:{reason}:{book_id}")

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
        _mock_listing, mock_repo, alert, book = _bd_scenario(None)
        # Alerte ISBN : pas de couverture de série.
        alert.isbn = "9782205078054"
        sibling = MagicMock()
        sibling.id = uuid.uuid4()
        sibling.title = "Blacksad - Tome 5"
        sibling.market_cache = None
        alert.alert_books = [MagicMock(book=book), MagicMock(book=sibling)]
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

    # Seul le livre résolu est coté — l'alerte ISBN n'a pas de série à couvrir.
    assert f"price:alert_sent:{book.id}" in calls
    assert f"price:alert_sent:{sibling.id}" not in calls
    assert schedule.call_count == 1


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
        ),
        pytest.raises(RuntimeError, match="telegram down"),
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
        repo_cls.return_value.resolve_book_for_listing = AsyncMock(return_value=(None, None, None))
        result = await use_case._resolve_bd_market_comparison(listing, alert)

    assert result is None
    scheduled.assert_called_once()
    assert scheduled.call_args.kwargs["name"] == f"bnf_link_{alert.id}"


@pytest.mark.asyncio
async def test_bd_series_alert_relaunches_the_whole_series_discovery(
    mock_db: AsyncMock,
) -> None:
    """Une annonce non résolue d'une alerte sans ISBN relance la **série** entière.

    Avant, ce chemin ne demandait qu'un titre : les tomes d'une série déjà créée
    n'étaient donc jamais complétés après la création. `series=not alert.isbn`
    vaut True ici, et la tâche reste différée — l'envoi n'attend rien.
    """
    use_case = ProcessListingUseCase(db=mock_db)
    listing = MagicMock(title="Blacksad - Tome 9", isbn13=None)
    alert = MagicMock(id=uuid.uuid4(), isbn=None, name="Blacksad")

    with (
        patch("app.use_cases.ingest_listing.BookRepository") as repo_cls,
        patch("app.use_cases.ingest_listing.BookCatalogService"),
        patch("app.use_cases.ingest_listing.link_books_from_bnf") as link,
        patch("app.use_cases.ingest_listing.safe_create_task") as scheduled,
    ):
        repo_cls.return_value.resolve_book_for_listing = AsyncMock(return_value=(None, None, None))
        await use_case._resolve_bd_market_comparison(listing, alert)

    link.assert_called_once()
    assert link.call_args.kwargs["series"] is True
    scheduled.assert_called_once()


@pytest.mark.asyncio
async def test_bd_alert_with_an_isbn_keeps_its_unitary_path(mock_db: AsyncMock) -> None:
    """Une alerte à ISBN ne relance pas de découverte de série : elle reste unitaire.

    La branche à ISBN résout son livre par le code ; le drapeau `series=not alert.isbn`
    ne doit donc jamais être posé à False sur une découverte de série — il n'y a
    simplement pas de telle découverte pour ces alertes.
    """
    use_case = ProcessListingUseCase(db=mock_db)
    listing = MagicMock(title="Les Indes fourbes", isbn13=None)
    alert = MagicMock(id=uuid.uuid4(), isbn="9782889010851", name="Les Indes fourbes")

    with (
        patch("app.use_cases.ingest_listing.BookRepository") as repo_cls,
        patch("app.use_cases.ingest_listing.BookCatalogService") as catalog_cls,
        patch("app.use_cases.ingest_listing.link_books_from_bnf") as link,
        patch("app.use_cases.ingest_listing.safe_create_task") as scheduled,
    ):
        repo_cls.return_value.get_book_for_alert = AsyncMock(return_value=None)
        repo_cls.return_value.get_by_isbn13 = AsyncMock(return_value=None)
        catalog_cls.return_value.add_book = AsyncMock(return_value=None)
        await use_case._resolve_bd_market_comparison(listing, alert)

    link.assert_not_called()
    scheduled.assert_not_called()


@pytest.mark.asyncio
async def test_bd_alert_without_a_resolvable_book_announces_the_search(
    mock_db: AsyncMock,
) -> None:
    """Sans livre résolu, l'alerte annonce la recherche, jamais le silence."""
    from app.services.market_price_service import SEARCHING_BOOK_LINE

    mock_send_alert = AsyncMock(return_value=True)
    scheduled: list[str] = []

    def _close_task(coro, **_kwargs):
        scheduled.append(_kwargs.get("name", ""))
        coro.close()
        return MagicMock()

    with (
        patch("app.api.routes.ingest.send_listing_alert", mock_send_alert),
        patch(
            "app.db.repositories.listing_repo.ListingRepository.mark_notified",
            new_callable=AsyncMock,
        ),
        patch("app.db.session.AsyncSessionLocal"),
        patch("app.use_cases.ingest_listing.safe_create_task", side_effect=_close_task),
        patch("app.use_cases.ingest_listing.schedule_book_pricing"),
    ):
        use_case = ProcessListingUseCase(db=mock_db)
        _listing, mock_repo, alert, _book = _bd_scenario(None)
        alert.alert_books = []
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
    assert kwargs["market_comparison_line"] == SEARCHING_BOOK_LINE
    # La ligne dit vrai : la résolution de fond part bien pour cet ouvrage.
    assert any(name.startswith("bd_catalog_") for name in scheduled)


@pytest.mark.asyncio
async def test_bd_alert_records_the_cote_state_it_shipped(mock_db: AsyncMock) -> None:
    """La mesure porte sur l'état réellement transmis, et part après l'envoi."""
    from app.use_cases import ingest_listing as mod

    mock_send_alert = AsyncMock(return_value=True)
    seen_states: list[str] = []
    scheduled: list[str] = []

    async def _noop() -> None:
        return None

    def _record(state: str) -> object:
        seen_states.append(state)
        return _noop()

    def _capture(coro, **_kwargs):
        scheduled.append(_kwargs.get("name", ""))
        coro.close()
        return MagicMock()

    with (
        patch("app.api.routes.ingest.send_listing_alert", mock_send_alert),
        patch(
            "app.db.repositories.listing_repo.ListingRepository.mark_notified",
            new_callable=AsyncMock,
        ),
        patch("app.db.session.AsyncSessionLocal"),
        patch.object(mod, "record_sent_cote_state", new=MagicMock(side_effect=_record)),
        patch.object(mod, "safe_create_task", side_effect=_capture),
        patch.object(mod, "schedule_book_pricing"),
    ):
        use_case = ProcessListingUseCase(db=mock_db)
        _listing, mock_repo, _alert, _book = _bd_scenario(None)
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

    # Livre lié sans cote : l'alerte a annoncé « cote en cours », et c'est cet état
    # qui est mesuré.
    assert seen_states == ["unavailable"]
    assert any(name.startswith("cote_state_") for name in scheduled)


class _FakeSession:
    """Session simulée dont le contexte asynchrone est réutilisé par les deux tests."""

    async def __aenter__(self) -> AsyncMock:
        return AsyncMock()

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_catalog_resolution_prices_the_book_it_links() -> None:
    """La résolution de fond cote, après commit, le livre qu'elle vient de lier."""
    from app.use_cases import ingest_listing as mod

    book = MagicMock()
    book.id = uuid.uuid4()

    with (
        patch("app.db.session.AsyncSessionLocal", MagicMock(return_value=_FakeSession())),
        patch.object(
            mod.ProcessListingUseCase,
            "_resolve_bd_market_comparison",
            new=AsyncMock(return_value=book),
        ),
        patch.object(mod, "BookRepository") as repo_cls,
        patch.object(mod, "has_usable_price", return_value=False),
        patch.object(mod, "schedule_book_pricing") as schedule,
    ):
        repo_cls.return_value.get_by_id = AsyncMock(return_value=MagicMock(market_cache=None))
        await mod._link_bd_catalog_background(
            alert_id=uuid.uuid4(),
            alert_name="Blacksad",
            alert_isbn=None,
            listing_title="Blacksad - Tome 6",
            listing_description=None,
            listing_isbn13=None,
        )

    schedule.assert_called_once_with(book.id, reason="bd_resolved")


@pytest.mark.asyncio
async def test_catalog_resolution_leaves_an_already_priced_book_alone() -> None:
    """Un livre déjà coté n'est pas recoté : la résolution ne cote que les manquants."""
    from app.use_cases import ingest_listing as mod

    book = MagicMock()
    book.id = uuid.uuid4()

    with (
        patch("app.db.session.AsyncSessionLocal", MagicMock(return_value=_FakeSession())),
        patch.object(
            mod.ProcessListingUseCase,
            "_resolve_bd_market_comparison",
            new=AsyncMock(return_value=book),
        ),
        patch.object(mod, "BookRepository") as repo_cls,
        patch.object(mod, "has_usable_price", return_value=True),
        patch.object(mod, "schedule_book_pricing") as schedule,
    ):
        repo_cls.return_value.get_by_id = AsyncMock(
            return_value=MagicMock(market_cache=MagicMock())
        )
        await mod._link_bd_catalog_background(
            alert_id=uuid.uuid4(),
            alert_name="Blacksad",
            alert_isbn=None,
            listing_title="Blacksad - Tome 6",
            listing_description=None,
            listing_isbn13=None,
        )

    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_bnf_link_queries_the_alert_name_and_prices_the_linked_book() -> None:
    """La BNF est interrogée par nom d'alerte (+ auteur), jamais par titre d'annonce."""
    from app.services.book_metadata_service import CatalogueVerdict
    from app.use_cases import ingest_listing as mod

    meta = MagicMock(isbn13="9782369904151", author="Un Auteur", publisher="Un Éditeur")
    book = MagicMock()
    book.id = uuid.uuid4()

    with (
        patch("app.services.book_metadata_service.BookMetadataService") as service_cls,
        patch("app.db.session.AsyncSessionLocal", MagicMock(return_value=_FakeSession())),
        patch.object(mod, "BookRepository") as repo_cls,
        patch.object(mod, "BookCatalogService"),
        patch.object(mod, "schedule_book_pricing") as schedule,
    ):
        service_cls.return_value.lookup_by_title_and_author = AsyncMock(
            return_value=CatalogueVerdict(consulted=("bnf",), notices=(meta,))
        )
        repo_cls.return_value.get_by_isbn13 = AsyncMock(return_value=book)
        repo_cls.return_value.link_alert_to_book = AsyncMock()
        repo_cls.return_value.get_by_id = AsyncMock(return_value=MagicMock(market_cache=None))

        await mod.link_books_from_bnf("The Grocery", uuid.uuid4(), author="Un Auteur")

    service_cls.return_value.lookup_by_title_and_author.assert_awaited_once_with(
        "The Grocery par Un Auteur"
    )
    schedule.assert_called_once_with(book.id, reason="bnf_link")


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_for_prices", [False, True])
async def test_bnf_link_for_a_series_links_and_prices_every_tome(wait_for_prices: bool) -> None:
    """`series=True` lie chaque tome de la série et ne cote que ceux sans cote."""
    from app.db.models.book_market_cache import BookMarketCache
    from app.services.book_metadata_service import CatalogueVerdict
    from app.use_cases import ingest_listing as mod

    priced = MagicMock(isbn13="9782369900011", title="Blacksad Tome 1", author="A", publisher="P")
    already_priced = MagicMock(
        isbn13="9782369900028", title="Blacksad Tome 2", author="A", publisher="P"
    )
    books = {}
    for notice in (priced, already_priced):
        book = MagicMock(title="Blacksad")
        book.id = uuid.uuid4()
        books[notice.isbn13] = book

    def _get_isbn(isbn13: str) -> object:
        return books.get(isbn13)

    with (
        patch("app.services.book_metadata_service.BookMetadataService") as service_cls,
        patch("app.db.session.AsyncSessionLocal", MagicMock(return_value=_FakeSession())),
        patch.object(mod, "BookRepository") as repo_cls,
        patch.object(mod, "BookCatalogService"),
        patch.object(mod, "schedule_book_pricing") as schedule,
        patch.object(mod, "price_book_now", new_callable=AsyncMock) as price_now,
    ):
        service_cls.return_value.lookup_series_notices = AsyncMock(
            return_value=CatalogueVerdict(consulted=("bnf",), notices=(priced, already_priced))
        )
        repo_cls.return_value.get_by_isbn13 = AsyncMock(side_effect=_get_isbn)
        repo_cls.return_value.link_alert_to_book = AsyncMock()
        usable_cache = BookMarketCache(
            id=uuid.uuid4(),
            book_id=books[already_priced.isbn13].id,
            lowest_delivered_price_cents=1200,
            comparison_status=ComparisonStatus.AVAILABLE,
            historical_low_1y_cents=1000,
        )
        repo_cls.return_value.get_by_id = AsyncMock(
            side_effect=lambda book_id: MagicMock(
                market_cache=(None if book_id == books[priced.isbn13].id else usable_cache)
            )
        )

        await mod.link_books_from_bnf(
            "Blacksad", uuid.uuid4(), author="A", series=True, wait_for_prices=wait_for_prices
        )

    service_cls.return_value.lookup_series_notices.assert_awaited_once_with(
        "Blacksad", author_hint="A"
    )
    # Un seul tome est coté : celui qui n'avait pas de cote utilisable.
    if wait_for_prices:
        schedule.assert_not_called()
        price_now.assert_awaited_once_with(books[priced.isbn13].id, reason="bnf_link")
    else:
        schedule.assert_called_once_with(books[priced.isbn13].id, reason="bnf_link")
        price_now.assert_not_awaited()
    assert books[priced.isbn13].title == "Blacksad Tome 1"


@pytest.mark.asyncio
async def test_a_conclusive_search_writes_its_verdict_on_the_alert() -> None:
    """Les trois issues d'une résolution de fond : verdict posé, effacé, ou silence.

    Le verdict n'est écrit que quand il est concluant. Une notice arrivée l'efface ;
    une autorité muette ne l'écrit pas **et** n'efface pas ce qu'on savait — un
    silence ne prouve rien, dans aucun sens (ADR-041).
    """
    from app.services.book_metadata_service import CatalogueVerdict
    from app.use_cases import ingest_listing as mod

    notice = MagicMock(isbn13="9782369904151", author="A", publisher="P", title="T")
    cases = {
        "aucune source ne le connaît": CatalogueVerdict(consulted=("bnf",)),
        "une autorité s'est tue": CatalogueVerdict(consulted=("bnf",), unavailable=("bnf",)),
        "une notice est arrivée": CatalogueVerdict(consulted=("bnf",), notices=(notice,)),
    }

    with (
        patch("app.services.book_metadata_service.BookMetadataService") as service_cls,
        patch("app.db.session.AsyncSessionLocal", MagicMock(return_value=_FakeSession())),
        patch.object(mod, "BookRepository") as repo_cls,
        patch.object(mod, "BookCatalogService"),
        patch.object(mod, "AlertRepository") as alert_repo_cls,
        patch.object(mod, "schedule_book_pricing"),
    ):
        book = MagicMock()
        book.id = uuid.uuid4()
        repo_cls.return_value.get_by_isbn13 = AsyncMock(return_value=book)
        repo_cls.return_value.get_by_id = AsyncMock(
            return_value=MagicMock(market_cache=MagicMock())
        )
        repo_cls.return_value.link_alert_to_book = AsyncMock()
        recorder = AsyncMock()
        alert_repo_cls.return_value.set_book_search_exhausted = recorder

        alert_id = uuid.uuid4()
        service_cls.return_value.lookup_by_title_and_author = AsyncMock(
            return_value=cases["aucune source ne le connaît"]
        )
        await mod.link_books_from_bnf("Absolute Martian Manhunter", alert_id)
        recorded_alert, recorded_at = recorder.await_args.args
        assert recorded_alert == alert_id
        assert isinstance(recorded_at, datetime), (
            "le verdict se date, il ne se pose pas comme un drapeau"
        )

        recorder.reset_mock()
        service_cls.return_value.lookup_by_title_and_author = AsyncMock(
            return_value=cases["une autorité s'est tue"]
        )
        await mod.link_books_from_bnf("Absolute Martian Manhunter", alert_id)
        recorder.assert_not_awaited()

        recorder.reset_mock()
        service_cls.return_value.lookup_by_title_and_author = AsyncMock(
            return_value=cases["une notice est arrivée"]
        )
        await mod.link_books_from_bnf("Absolute Martian Manhunter", alert_id)
        assert recorder.await_args.args == (alert_id, None), (
            "une notice liée contredit le verdict : elle l'efface"
        )


@pytest.mark.asyncio
async def test_the_alert_of_an_uncatalogued_series_says_it(mock_db: AsyncMock) -> None:
    """Aucun livre, et le verdict en base : l'alerte le dit, elle n'annonce plus rien.

    C'est la ligne que l'utilisateur recevait à chaque annonce d'une série que
    personne ne catalogue : « recherche de l'ouvrage en cours » y était faux, la
    recherche étant terminée, et sa réponse tenant en cette phrase.
    """
    from app.services.market_health import classify_sent_cote_state
    from app.services.market_price_service import NO_CATALOGUE_LINE

    mock_send_alert = AsyncMock(return_value=True)
    with (
        patch("app.api.routes.ingest.send_listing_alert", mock_send_alert),
        patch(
            "app.db.repositories.listing_repo.ListingRepository.mark_notified",
            new_callable=AsyncMock,
        ),
        patch("app.db.session.AsyncSessionLocal"),
        patch("app.use_cases.ingest_listing.schedule_book_pricing"),
    ):
        use_case = ProcessListingUseCase(db=mock_db)
        _mock_listing, mock_repo, alert, _book = _bd_scenario(None)
        alert.alert_books = []
        alert.book_search_exhausted_at = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)
        use_case.listing_repo = mock_repo
        payload = ListingIngestPayload(
            source_name="vinted",
            external_id="test-456",
            url="https://vinted.fr/item-456",
            title="Absolute Martian Manhunter",
            category_slug="bd_comics",
            item_price_cents=900,
        )
        await use_case.execute(payload, raw_payload={})

    line = mock_send_alert.call_args.kwargs["market_comparison_line"]
    assert line == NO_CATALOGUE_LINE
    # L'audit d'envoi ne compte pas cette ligne comme une cote chiffrée : elle
    # n'en est pas une, et la compter « real » ferait croire à une couverture.
    assert classify_sent_cote_state(line) == "uncatalogued"
