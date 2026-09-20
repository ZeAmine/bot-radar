"""Tests unitaires pour ProcessListingUseCase et AsyncEnrichListingUseCase."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

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

