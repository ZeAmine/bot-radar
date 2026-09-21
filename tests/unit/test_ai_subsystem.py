"""Tests unitaires pour le sous-système IA (DeepSeek-V4.1-Flash)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.db.models.listing import Listing
from app.services.ai.client import DeepSeekClient
from app.services.ai.modules.bundle_decomposition import bundle_decomposition_module
from app.services.ai.modules.hidden_condition import hidden_condition_module
from app.services.ai.modules.natural_add import natural_add_module
from app.services.ai.modules.negotiation import negotiation_module
from app.services.ai.modules.price_glitch import price_glitch_module
from app.services.ai.modules.profit_scoring import profit_scoring_module
from app.services.ai.modules.vision_guard import vision_guard_module
from app.services.ai.pipeline import AIPostAlertPipeline
from app.services.ai.registry import AIRegistry


@pytest.fixture
def sample_listing():
    return Listing(
        id=uuid.uuid4(),
        source_name="vinted",
        external_id="987654321",
        title="Lot Manga One Piece tomes 1 à 10",
        description=(
            "Bande dessinée sans usure mais tome 3 avec tranche jaunie et accroc sur la coiffe."
        ),
        condition="Très bon état",
        item_price_cents=2500,
        estimated_total_cents=2950,
        photos=["https://images1.vinted.net/item1.jpg", "https://images1.vinted.net/item2.jpg"],
        category_slug="bd_comics",
        url="https://www.vinted.fr/items/987654321-lot-one-piece",
    )


@pytest.mark.asyncio
async def test_pipeline_skips_llm_when_client_unconfigured(sample_listing):
    """Un client non configuré ne doit pas lever NameError ni appeler DeepSeek."""
    client = MagicMock(spec=DeepSeekClient)
    client.is_configured = False
    client.generate_json = AsyncMock()
    pipeline = AIPostAlertPipeline(client=client)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    sample_listing.photos = []

    with (
        patch("app.services.ai.pipeline.asyncio.sleep", new_callable=AsyncMock),
        patch("app.services.ai.pipeline.logger.exception") as log_exception,
    ):
        await pipeline.enrich_alert(
            bot=bot,
            chat_id=1,
            message_id=10,
            listing=sample_listing,
            alert=MagicMock(isbn="9780000000000"),
        )

    log_exception.assert_not_called()
    client.generate_json.assert_not_awaited()
    bot.send_message.assert_not_awaited()


def test_deepseek_client_unconfigured():
    """Un client sans clé API doit renvoyer False pour is_configured."""
    client = DeepSeekClient(api_key="")
    assert client.is_configured is False


def test_deepseek_clean_and_parse_json():
    """Vérifie le nettoyage des blocs markdown ```json ... ```."""
    raw = '```json\n{"test": 123, "valid": true}\n```'
    parsed = DeepSeekClient._clean_and_parse_json(raw)
    assert parsed == {"test": 123, "valid": True}

    raw_bad = "Ce n'est pas du JSON"
    assert DeepSeekClient._clean_and_parse_json(raw_bad) is None


@pytest.mark.asyncio
async def test_natural_add_parsing():
    """Vérifie que le module natural_add parse correctement une réponse JSON simulée."""
    mock_client = MagicMock(spec=DeepSeekClient)
    mock_client.is_configured = True
    mock_client.generate_json = AsyncMock(
        return_value={
            "name": "One Piece tomes 1 à 10",
            "category_slug": "bd_comics",
            "include_keywords": ["One Piece", "tome"],
            "exclude_keywords": ["figurine"],
            "max_price_cents": 3500,
            "conditions": ["Neuf avec étiquette", "Très bon état"],
            "explanation": "Alerte pour tomes 1 à 10",
        }
    )

    module = natural_add_module
    with patch.object(module, "client", mock_client):
        res = await module.parse_prompt("One Piece tomes 1 à 10 max 35€ sans figurine")
        assert res is not None
        assert res.name == "One Piece tomes 1 à 10"
        assert res.category_slug == "bd_comics"
        assert res.max_price_cents == 3500
        assert "figurine" in res.exclude_keywords


@pytest.mark.asyncio
async def test_natural_add_cleaning_price_suffix():
    """Vérifie que tout suffixe de prix résiduel dans le nom est nettoyé."""
    mock_client = MagicMock(spec=DeepSeekClient)
    mock_client.is_configured = True
    mock_client.generate_json = AsyncMock(
        return_value={
            "name": "Arabe du futur - max 7€",
            "category_slug": "bd_comics",
            "include_keywords": ["Arabe du futur - max 7€"],
            "exclude_keywords": ["figurine", "coffret vide"],
            "max_price_cents": 700,
        }
    )

    module = natural_add_module
    with patch.object(module, "client", mock_client):
        res = await module.parse_prompt("Arabe du futur max 7€")
        assert res is not None
        assert res.name == "Arabe du futur"
        assert res.include_keywords == ["Arabe du futur"]
        assert "figurine" in res.exclude_keywords
        assert "coffret vide" in res.exclude_keywords
        assert res.max_price_cents == 700


@pytest.mark.asyncio
async def test_keyword_variants_module_variants_and_exclusions():
    """Vérifie la génération simultanée de variantes et exclusions intelligentes."""
    from app.services.ai.modules.keyword_variants import keyword_variants_module

    mock_client = MagicMock(spec=DeepSeekClient)
    mock_client.is_configured = True
    mock_client.generate_json = AsyncMock(
        return_value={
            "variants": ["arabe du future", "larabe du futur"],
            "exclusions": ["figurine", "coffret vide", "poster"],
        }
    )

    with patch.object(keyword_variants_module, "client", mock_client):
        variants, exclusions = await keyword_variants_module.generate_variants_and_exclusions(
            "L'Arabe du futur", category_slug="bd_comics"
        )
        assert "arabe du future" in variants
        assert "larabe du futur" in variants
        assert "figurine" in exclusions
        assert "coffret vide" in exclusions


def test_hidden_condition_module(sample_listing):
    """Vérifie le parsing et le formatage des défauts cachés."""
    chunk = hidden_condition_module.build_prompt_chunk(sample_listing, None)
    assert chunk is not None
    assert chunk[0] == "hidden_condition"

    raw_json = {
        "has_hidden_defects": True,
        "real_condition_summary": "Tranche jaunie et accroc sur la coiffe",
        "defects_found": ["tranche jaunie", "accroc coiffe"],
        "severity": "moyen",
    }
    result = hidden_condition_module.parse_chunk_result(raw_json, sample_listing, None)
    assert result.has_hidden_defects is True
    assert "tranche jaunie" in result.defects_found

    html = hidden_condition_module.format_telegram_html(result)
    assert "Attention état réel" in html
    assert "Tranche jaunie" in html


def test_price_glitch_module(sample_listing):
    """Vérifie le parsing et le formatage des anomalies de prix."""
    raw_json = {
        "is_glitch": True,
        "estimated_market_value_cents": 6000,
        "discount_percent": 60,
        "reason": "Vendu 25€ au lieu de 60€ sur le marché",
    }
    result = price_glitch_module.parse_chunk_result(raw_json, sample_listing, None)
    assert result.is_glitch is True
    assert result.discount_percent == 60

    html = price_glitch_module.format_telegram_html(result)
    assert "ANOMALIE DE PRIX DÉTECTÉE" in html
    assert "60.00 €" in html


def test_bundle_decomposition_module(sample_listing):
    """Vérifie la décomposition de lot et calcul du prix unitaire."""
    raw_json = {
        "is_bundle": True,
        "items_detected": ["One Piece Tome 1", "One Piece Tome 2", "One Piece Tome 3"],
        "target_item_present": True,
        "summary": "Lot de 3 tomes",
    }
    result = bundle_decomposition_module.parse_chunk_result(raw_json, sample_listing, None)
    assert result.is_bundle is True
    assert len(result.items_detected) == 3
    # 2500 cents // 3 items = 833 cents (~8.33 €)
    assert result.estimated_price_per_item_cents == 833

    html = bundle_decomposition_module.format_telegram_html(result)
    assert html is None

    extracted_tomes = bundle_decomposition_module.extract_tomes(result.items_detected)
    assert extracted_tomes == [1, 2, 3]


def test_profit_scoring_module(sample_listing):
    """Vérifie le calcul d'indice pépite et son seuil d'affichage."""
    raw_json = {
        "estimated_resale_cents": 5500,
        "net_profit_cents": 2550,
        "pepite_score": 8.5,
        "verdict": "Excellente marge nette",
    }
    result = profit_scoring_module.parse_chunk_result(raw_json, sample_listing, None)
    assert result.pepite_score == 8.5

    html = profit_scoring_module.format_telegram_html(result)
    assert "Prix d'achat TTC" in html
    assert "Revente estimée : ~55,00 €" in html
    assert "Marge nette : <b>+25,50 €</b>" in html
    assert "Indice pépite : 8,5/10" in html
    assert "Excellente marge nette" in html

    # Vérifier l'exclusion des livres / BDs (gérés par Chasse-aux-Livres)
    assert profit_scoring_module.build_prompt_chunk(sample_listing, None) is None

    # Vérifier la génération de prompt dynamique pour produits non-livres (Tech, Gaming, etc.)
    tech_listing = Listing(
        id=uuid.uuid4(),
        title="Nintendo Switch OLED Blanche complète",
        category_slug="tech",
        item_price_cents=18000,
        estimated_total_cents=18500,
        condition="Très bon état",
        description="Vendue avec boîte d'origine et tous les accessoires.",
    )
    chunk = profit_scoring_module.build_prompt_chunk(tech_listing, None)
    assert chunk is not None
    assert chunk[0] == "profit_scoring"
    assert "Nintendo Switch OLED" in chunk[1]
    assert "185.00 €" in chunk[1]
    assert "marché de l'occasion en France" in chunk[1]


@pytest.mark.asyncio
async def test_negotiation_module_fallback(sample_listing):
    """Vérifie le fallback algorithmique de négociation si l'IA n'est pas connectée."""
    mock_client = MagicMock(spec=DeepSeekClient)
    mock_client.is_configured = False

    module = negotiation_module
    with patch.object(module, "client", mock_client):
        res = await module.generate_offer(sample_listing)
        assert res is not None
        assert res.suggested_offer_euros < 25.0
        assert "Bonjour" in res.message

        html = module.format_telegram_html(res)
        assert "<code>" in html


def test_ai_registry():
    """Vérifie le registre et l'isolation des modules."""
    registry = AIRegistry()
    assert registry.get_module("hidden_condition") is not None
    assert registry.get_module("price_glitch") is not None
    assert price_glitch_module.is_enabled is False

    active_posts = registry.get_active_post_alert_modules()
    assert len(active_posts) >= 2
    assert "price_glitch" not in [m.name for m in active_posts]

    registry.unregister("hidden_condition")
    assert registry.get_module("hidden_condition") is None


@pytest.mark.asyncio
async def test_vision_guard_multi_photos(sample_listing):
    """Vérifie l'analyse multi-photos de VisionGuard."""
    mock_client = MagicMock(spec=DeepSeekClient)
    mock_client.is_configured = True
    mock_client.generate_json = AsyncMock(
        return_value={
            "has_visual_defects": True,
            "is_suspicious": False,
            "summary": "Accroc visible sur la tranche",
            "inspections": [
                {
                    "photo_index": 2,
                    "defects": ["accroc sur la tranche"],
                    "spines_detected": [],
                    "is_suspicious_or_empty": False,
                    "confidence": 0.9,
                }
            ],
        }
    )

    module = vision_guard_module
    with (
        patch.object(module, "client", mock_client),
        patch.object(settings, "AI_FEATURE_VISION_GUARD", True),
    ):
        res = await module.analyze_listing_photos(sample_listing)
        assert res is not None
        assert res.has_visual_defects is True
        assert len(res.inspections) == 1

        html = module.format_telegram_html(res)
        assert "Défauts visuels repérés" in html
        assert "Photo 2" in html
