"""Tests unitaires complets pour DeepSeekProvider et son cache Redis."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.core.exceptions import (
    DeepSeekAPIError,
    DeepSeekTimeoutError,
    DeepSeekValidationError,
)
from app.domain.entities.analysis import ListingAnalysisResult, RealConditionAssessment
from app.domain.enums import CategorySlug
from app.providers.llm.deepseek_provider import DeepSeekProvider


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Fixture d'un client Redis asynchrone mocké."""
    redis = AsyncMock()
    redis.get.return_value = None
    redis.setex.return_value = True
    return redis


@pytest.fixture
def mock_http_client() -> AsyncMock:
    """Fixture d'un client HTTPx asynchrone mocké."""
    client = AsyncMock(spec=httpx.AsyncClient)
    return client


@pytest.fixture(autouse=True)
def enable_ai_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Active l'IA et configure une clé API factice pour les tests."""
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", SecretStr("test-secret-key"))
    monkeypatch.setattr(settings, "DEEPSEEK_MODEL", "deepseek-flash")
    monkeypatch.setattr(settings, "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")


@pytest.mark.asyncio
async def test_deepseek_provider_cache_hit(
    mock_http_client: AsyncMock,
    mock_redis: AsyncMock,
) -> None:
    """Vérifie qu'un cache HIT retourne directement l'objet sans appeler l'API."""
    cached_obj = ListingAnalysisResult(
        real_condition=RealConditionAssessment.VERY_GOOD,
        is_lot_incomplete=False,
        is_scam_or_trap=False,
        risk_score=5,
        analysis_summary="Très bon état, lot complet.",
    )
    mock_redis.get.return_value = cached_obj.model_dump_json()

    provider = DeepSeekProvider(http_client=mock_http_client, redis_client=mock_redis)
    result = await provider.analyze_listing(
        title="One Piece Tome 1",
        description="Parfait état, lu une fois",
        price_cents=500,
        declared_condition="Très bon état",
        category=CategorySlug.BD_COMICS,
    )

    assert result == cached_obj
    mock_redis.get.assert_awaited_once()
    mock_http_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_deepseek_provider_cache_miss_and_success(
    mock_http_client: AsyncMock, mock_redis: AsyncMock
) -> None:
    """Vérifie le flux complet : cache MISS -> appel API -> parsing Pydantic -> écriture cache."""
    mock_redis.get.return_value = None

    api_response_data = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"real_condition": "acceptable", '
                        '"is_lot_incomplete": true, '
                        '"is_scam_or_trap": false, '
                        '"risk_score": 35, '
                        '"analysis_summary": "Manque le tome 3 dans le lot."}'
                    )
                }
            }
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = api_response_data
    mock_resp.raise_for_status.return_value = None
    mock_http_client.post.return_value = mock_resp

    provider = DeepSeekProvider(http_client=mock_http_client, redis_client=mock_redis)
    result = await provider.analyze_listing(
        title="Lot Manga One Piece 1 à 5",
        description="Vends lot tomes 1, 2, 4, 5. Tome 3 vendu à part.",
        price_cents=1500,
        declared_condition="Bon état",
        category=CategorySlug.BD_COMICS,
    )

    assert result is not None
    assert result.real_condition == RealConditionAssessment.ACCEPTABLE
    assert result.is_lot_incomplete is True
    assert result.is_scam_or_trap is False
    assert result.risk_score == 35
    assert "Manque le tome 3" in result.analysis_summary

    # Vérification de l'écriture en cache
    mock_redis.setex.assert_awaited_once()


@pytest.mark.asyncio
async def test_deepseek_provider_timeout_graceful_fallback(
    mock_http_client: AsyncMock, mock_redis: AsyncMock
) -> None:
    """Vérifie que le timeout déclenche un fallback gracieux (None) par défaut."""
    mock_redis.get.return_value = None
    mock_http_client.post.side_effect = httpx.TimeoutException("Délai dépassé")

    provider = DeepSeekProvider(
        http_client=mock_http_client,
        redis_client=mock_redis,
        raise_exceptions=False,
    )
    result = await provider.analyze_listing(
        title="Article test",
        description="Description",
        price_cents=1000,
        declared_condition="Bon état",
        category=CategorySlug.TECH,
    )

    assert result is None


@pytest.mark.asyncio
async def test_deepseek_provider_timeout_raises_when_configured(
    mock_http_client: AsyncMock, mock_redis: AsyncMock
) -> None:
    """Vérifie qu'une exception typée DeepSeekTimeoutError est levée si raise_exceptions=True."""
    mock_redis.get.return_value = None
    mock_http_client.post.side_effect = httpx.TimeoutException("Délai dépassé")

    provider = DeepSeekProvider(
        http_client=mock_http_client,
        redis_client=mock_redis,
        raise_exceptions=True,
    )

    with pytest.raises(DeepSeekTimeoutError):
        await provider.analyze_listing(
            title="Article test",
            description="Description",
            price_cents=1000,
            declared_condition="Bon état",
            category=CategorySlug.TECH,
        )


@pytest.mark.asyncio
async def test_deepseek_provider_http_error_handling(
    mock_http_client: AsyncMock, mock_redis: AsyncMock
) -> None:
    """Vérifie la gestion d'une erreur HTTP 500 de l'API DeepSeek."""
    mock_redis.get.return_value = None
    req = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
    resp = httpx.Response(status_code=500, request=req, text="Internal Server Error")
    mock_http_client.post.side_effect = httpx.HTTPStatusError(
        "Erreur 500", request=req, response=resp
    )

    provider = DeepSeekProvider(
        http_client=mock_http_client,
        redis_client=mock_redis,
        raise_exceptions=True,
    )

    with pytest.raises(DeepSeekAPIError) as exc_info:
        await provider.analyze_listing(
            title="Article test",
            description="Description",
            price_cents=1000,
            declared_condition="Bon état",
            category=CategorySlug.TECH,
        )

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_deepseek_provider_invalid_json_validation_error(
    mock_http_client: AsyncMock, mock_redis: AsyncMock
) -> None:
    """Vérifie la gestion d'un JSON non conforme au modèle Pydantic."""
    mock_redis.get.return_value = None
    api_response_data = {
        "choices": [
            {
                "message": {
                    # Manque real_condition et risk_score
                    "content": '{"is_scam_or_trap": false}'
                }
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = api_response_data
    mock_resp.raise_for_status.return_value = None
    mock_http_client.post.return_value = mock_resp

    provider = DeepSeekProvider(
        http_client=mock_http_client,
        redis_client=mock_redis,
        raise_exceptions=True,
    )

    with pytest.raises(DeepSeekValidationError):
        await provider.analyze_listing(
            title="Article test",
            description="Description",
            price_cents=1000,
            declared_condition="Bon état",
            category=CategorySlug.TECH,
        )


@pytest.mark.asyncio
async def test_deepseek_provider_disabled_ai(
    monkeypatch: pytest.MonkeyPatch, mock_http_client: AsyncMock, mock_redis: AsyncMock
) -> None:
    """Vérifie qu'aucun appel n'est effectué si l'IA est désactivée."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)

    provider = DeepSeekProvider(http_client=mock_http_client, redis_client=mock_redis)
    result = await provider.analyze_listing(
        title="Article test",
        description="Description",
        price_cents=1000,
        declared_condition="Bon état",
        category=CategorySlug.TECH,
    )

    assert result is None
    mock_redis.get.assert_not_called()
    mock_http_client.post.assert_not_called()


def test_deepseek_cache_key_determinism(
    mock_http_client: AsyncMock,
    mock_redis: AsyncMock,
) -> None:
    """Vérifie que la clé de cache est insensible à la casse et aux espaces superflus."""
    provider = DeepSeekProvider(http_client=mock_http_client, redis_client=mock_redis)

    key1 = provider.compute_cache_key("  Titre Test ", " Description  ", CategorySlug.BD_COMICS)
    key2 = provider.compute_cache_key("titre test", "description", CategorySlug.BD_COMICS)
    key3 = provider.compute_cache_key("Autre titre", "description", CategorySlug.BD_COMICS)

    assert key1 == key2
    assert key1 != key3
    assert key1.startswith("radar:deepseek:analysis:")
