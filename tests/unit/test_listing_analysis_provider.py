"""Tests unitaires du fournisseur d'analyse d'annonce et de son cache Redis.

Le fournisseur reçoit un `LLMClient` : ces tests n'ont plus de fournisseur dans leurs
dépendances, ce qui est exactement le contrat à préserver lors d'un changement de
modèle. Les exceptions HTTP sont simulées au niveau du client, car un adaptateur les
absorbe volontairement et rend `None`.
"""

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.core.exceptions import LLMAPIError, LLMTimeoutError, LLMValidationError
from app.domain.entities.analysis import ListingAnalysisResult, RealConditionAssessment
from app.domain.enums import CategorySlug
from app.providers.llm.listing_analysis import ListingAnalysisProvider

VALID_PAYLOAD = (
    '{"real_condition": "acceptable", "is_lot_incomplete": true, '
    '"is_scam_or_trap": false, "risk_score": 35, '
    '"analysis_summary": "Manque le tome 3 dans le lot."}'
)


class FakeLLM:
    """Moteur IA simulé : aucun nom de fournisseur, aucune forme de filaire."""

    def __init__(
        self,
        text: str | None = VALID_PAYLOAD,
        *,
        vendor: str = "fake-vendor",
        model: str = "fake-model",
        is_configured: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.vendor = vendor
        self.model = model
        self._is_configured = is_configured
        self._text = text
        self._error = error
        self.calls: list[dict[str, Any]] = []

    @property
    def is_configured(self) -> bool:
        return self._is_configured

    async def complete(self, **kwargs: Any) -> str | None:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._text


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Fixture d'un client Redis asynchrone mocké."""
    redis = AsyncMock()
    redis.get.return_value = None
    redis.setex.return_value = True
    return redis


@pytest.fixture(autouse=True)
def enable_ai_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Active l'IA et configure une clé API factice pour les tests."""
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_API_KEY", SecretStr("test-secret-key"))
    monkeypatch.setattr(settings, "AI_MODEL", "test-model")
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.example.test/v1")


@pytest.mark.asyncio
async def test_cache_hit_returns_without_calling_the_model(mock_redis: Any) -> None:
    """Un cache HIT rend l'objet memoïsé sans aucun appel au moteur."""
    cached = ListingAnalysisResult(
        real_condition=RealConditionAssessment.VERY_GOOD,
        is_lot_incomplete=False,
        is_scam_or_trap=False,
        risk_score=5,
        analysis_summary="Très bon état, lot complet.",
    )
    mock_redis.get.return_value = cached.model_dump_json()
    llm = FakeLLM()

    provider = ListingAnalysisProvider(llm=llm, redis_client=mock_redis)
    result = await provider.analyze_listing(
        title="One Piece Tome 1",
        description="Parfait état, lu une fois",
        price_cents=500,
        declared_condition="Très bon état",
        category=CategorySlug.BD_COMICS,
    )

    assert result == cached
    mock_redis.get.assert_awaited_once()
    assert llm.calls == []


@pytest.mark.asyncio
async def test_cache_miss_analyses_and_writes_cache(mock_redis: Any) -> None:
    """Cache MISS : appel au moteur, validation Pydantic, écriture en cache."""
    llm = FakeLLM()

    provider = ListingAnalysisProvider(llm=llm, redis_client=mock_redis)
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
    mock_redis.setex.assert_awaited_once()
    assert llm.calls[0]["system_prompt"]
    assert "One Piece" in llm.calls[0]["user_prompt"]


@pytest.mark.asyncio
async def test_image_urls_are_forwarded_to_the_model(mock_redis: Any) -> None:
    """Les photos de l'annonce sont transmises au moteur pour l'analyse multimodale."""
    llm = FakeLLM()

    provider = ListingAnalysisProvider(llm=llm, redis_client=mock_redis)
    await provider.analyze_listing(
        title="Console_switch",
        description="Avec manette",
        price_cents=15000,
        declared_condition="Très bon état",
        category=CategorySlug.TECH,
        image_urls=["https://images.example.test/1.jpg", "https://images.example.test/2.jpg"],
    )

    assert llm.calls[0]["image_urls"] == [
        "https://images.example.test/1.jpg",
        "https://images.example.test/2.jpg",
    ]


@pytest.mark.asyncio
async def test_timeout_falls_back_to_none(mock_redis: Any) -> None:
    """Un timeout rend `None` : l'analyse enrichit, elle ne bloque jamais l'alerte."""
    llm = FakeLLM(error=httpx.TimeoutException("Délai dépassé"))

    provider = ListingAnalysisProvider(llm=llm, redis_client=mock_redis)
    result = await provider.analyze_listing(
        title="Article test",
        description="Description",
        price_cents=1000,
        declared_condition="Bon état",
        category=CategorySlug.TECH,
    )

    assert result is None


@pytest.mark.asyncio
async def test_timeout_raises_typed_error_when_configured(mock_redis: Any) -> None:
    """`raise_exceptions=True` remonte une erreur typée, plus liée à un fournisseur."""
    llm = FakeLLM(error=httpx.TimeoutException("Délai dépassé"))

    provider = ListingAnalysisProvider(
        llm=llm, redis_client=mock_redis, raise_exceptions=True
    )
    with pytest.raises(LLMTimeoutError):
        await provider.analyze_listing(
            title="Article test",
            description="Description",
            price_cents=1000,
            declared_condition="Bon état",
            category=CategorySlug.TECH,
        )


@pytest.mark.asyncio
async def test_http_error_raises_typed_error_when_configured(mock_redis: Any) -> None:
    """Une erreur HTTP 500 remonte une erreur typée qui conserve le code de statut."""
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(status_code=500, request=request, text="Internal Server Error")
    llm = FakeLLM(
        error=httpx.HTTPStatusError("Erreur 500", request=request, response=response)
    )

    provider = ListingAnalysisProvider(
        llm=llm, redis_client=mock_redis, raise_exceptions=True
    )
    with pytest.raises(LLMAPIError):
        await provider.analyze_listing(
            title="Article test",
            description="Description",
            price_cents=1000,
            declared_condition="Bon état",
            category=CategorySlug.TECH,
        )


@pytest.mark.asyncio
async def test_non_json_response_falls_back_to_none(mock_redis: Any) -> None:
    """Une réponse qui n'est pas du JSON rend `None` sans lever."""
    llm = FakeLLM(text="Je ne peux pas répondre à cette question.")

    provider = ListingAnalysisProvider(llm=llm, redis_client=mock_redis)
    result = await provider.analyze_listing(
        title="Article test",
        description="Description",
        price_cents=1000,
        declared_condition="Bon état",
        category=CategorySlug.TECH,
    )

    assert result is None


@pytest.mark.asyncio
async def test_incomplete_json_raises_validation_error(mock_redis: Any) -> None:
    """Un JSON valide mais incomplet est refusé par le schéma."""
    llm = FakeLLM(text='{"is_scam_or_trap": false}')

    provider = ListingAnalysisProvider(
        llm=llm, redis_client=mock_redis, raise_exceptions=True
    )
    with pytest.raises(LLMValidationError):
        await provider.analyze_listing(
            title="Article test",
            description="Description",
            price_cents=1000,
            declared_condition="Bon état",
            category=CategorySlug.TECH,
        )


@pytest.mark.asyncio
async def test_disabled_ai_never_touches_redis_or_model(
    monkeypatch: pytest.MonkeyPatch, mock_redis: Any
) -> None:
    """`AI_ENABLED=False` court-circuite tout, cache compris."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    llm = FakeLLM()

    provider = ListingAnalysisProvider(llm=llm, redis_client=mock_redis)
    result = await provider.analyze_listing(
        title="Article test",
        description="Description",
        price_cents=1000,
        declared_condition="Bon état",
        category=CategorySlug.TECH,
    )

    assert result is None
    mock_redis.get.assert_not_called()
    assert llm.calls == []


@pytest.mark.asyncio
async def test_unconfigured_model_never_calls_redis(mock_redis: Any) -> None:
    """Sans clé API, le fournisseur s'arrête avant le cache : pas de lecture inutile."""
    llm = FakeLLM(is_configured=False)

    provider = ListingAnalysisProvider(llm=llm, redis_client=mock_redis)
    result = await provider.analyze_listing(
        title="Article test",
        description="Description",
        price_cents=1000,
        declared_condition="Bon état",
        category=CategorySlug.TECH,
    )

    assert result is None
    mock_redis.get.assert_not_called()
    assert llm.calls == []


def test_cache_key_ignores_case_and_padding(mock_redis: Any) -> None:
    """La clé est stable face à la casse et aux espaces superflus."""
    provider = ListingAnalysisProvider(llm=FakeLLM(), redis_client=mock_redis)

    key1 = provider.compute_cache_key("  Titre Test ", " Description  ", CategorySlug.BD_COMICS)
    key2 = provider.compute_cache_key("titre test", "description", CategorySlug.BD_COMICS)
    key3 = provider.compute_cache_key("Autre titre", "description", CategorySlug.BD_COMICS)

    assert key1 == key2
    assert key1 != key3
    assert key1.startswith("radar:llm:analysis:")


def test_cache_key_separates_models(mock_redis: Any) -> None:
    """Changer de moteur invalide le cache : sans cela, l'ancien modèle répondrait 7 jours."""
    old_model = ListingAnalysisProvider(
        llm=FakeLLM(vendor="openai-compatible", model="deepseek-flash"),
        redis_client=mock_redis,
    )
    new_model = ListingAnalysisProvider(
        llm=FakeLLM(vendor="anthropic", model="claude-haiku-4-5"),
        redis_client=mock_redis,
    )

    old_key = old_model.compute_cache_key("Titre", "Description", CategorySlug.BD_COMICS)
    new_key = new_model.compute_cache_key("Titre", "Description", CategorySlug.BD_COMICS)

    assert old_key != new_key
