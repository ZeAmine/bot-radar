"""Tests unitaires du sous-système IA, moteur simulé."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models.listing import Listing
from app.providers.llm.base import clean_and_parse_json
from app.services.ai.modules.natural_add import natural_add_module
from app.services.ai.modules.negotiation import negotiation_module
from app.services.ai.modules.profit_scoring import profit_scoring_module
from app.services.ai.pipeline import AIPostAlertPipeline
from app.services.ai.registry import AIRegistry
from app.services.book_metadata_service import BookMetadata, BookMetadataService, BookResolution
from tests.fakes_llm import FakeLLM

PANCHAUD_EDITION = BookMetadata(
    title="La couleur des choses",
    author="Martin Panchaud",
    publisher="Çà et là",
    isbn13="9782369903086",
    publication_year=2022,
    format_detail="1 vol. (224 p.) : ill. en coul. ; 24 cm",
)
PANCHAUD_ALTERNATIVE = BookMetadata(
    title="La couleur des choses",
    author="Martin Panchaud",
    publisher="Editions çà et là",
    isbn13="9782369904151",
    format_detail="1 vol. (224 p.) ; 19 cm",
)


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
    """Un moteur non configuré ne doit ni lever, ni appeler le réseau."""
    client = FakeLLM(enabled=False)
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
    assert client.calls == []
    bot.send_message.assert_not_awaited()


def test_engine_without_api_key_is_not_configured():
    """Un moteur sans clé API doit renvoyer False pour is_configured."""
    client = FakeLLM(api_key="  ")
    assert client.is_configured is False


def test_clean_and_parse_json_shared_by_every_engine():
    """Vérifie le nettoyage des blocs markdown ```json ... ```."""
    raw = '```json\n{"test": 123, "valid": true}\n```'
    parsed = clean_and_parse_json(raw)
    assert parsed == {"test": 123, "valid": True}

    raw_bad = "Ce n'est pas du JSON"
    assert clean_and_parse_json(raw_bad) is None


@pytest.mark.asyncio
async def test_natural_add_parsing():
    """Vérifie que le module natural_add parse correctement une réponse JSON simulée."""
    mock_client = FakeLLM(
        text={
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
    mock_client = FakeLLM(
        text={
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
async def test_natural_add_isbn_overruled_by_bnf():
    """L'ISBN proposé par le modèle est remplacé par celui confirmé par la BNF."""
    mock_client = FakeLLM(
        text={
            "name": "La couleur des choses",
            "category_slug": "bd_comics",
            # ISBN réel mais attribué à « Keeping two » (même éditeur)
            "isbn13": "9782369903062",
            "author": "Martin Panchaud",
            "publisher": "Ça et Là",
            "include_keywords": ["La couleur des choses"],
            "exclude_keywords": [],
            "max_price_cents": 1200,
            "is_series": False,
        }
    )

    bnf = MagicMock(spec=BookMetadataService)
    bnf.resolve_for_alert = AsyncMock(
        return_value=BookResolution(
            best=PANCHAUD_EDITION, alternatives=[PANCHAUD_ALTERNATIVE]
        )
    )
    bnf.verify_isbn = AsyncMock(return_value=None)

    with patch.object(natural_add_module, "client", mock_client), patch(
        "app.services.ai.modules.natural_add.BookMetadataService", return_value=bnf
    ):
        res = await natural_add_module.parse_prompt("la couleur des choses. Max 12 €.")

    assert res is not None
    assert res.isbn13 == "9782369903086"
    assert res.book_context is not None
    assert res.book_context.isbn13 == "9782369903086"
    assert res.book_context.publication_year == 2022
    assert res.book_context.format_detail == "1 vol. (224 p.) : ill. en coul. ; 24 cm"
    assert res.isbn_note is None
    bnf.verify_isbn.assert_awaited_once()


@pytest.mark.asyncio
async def test_natural_add_flags_openlibrary_fallback_provenance():
    """Un ISBN issu du repli OpenLibrary est signalé dans le récap : confiance moindre."""
    openlibrary_edition = BookMetadata(
        title="Klara and the Sun",
        author="Kazuo Ishiguro",
        publisher="Le Livre de Poche",
        isbn13="9782072909207",
        source="openlibrary",
    )
    mock_client = FakeLLM(
        text={
            "name": "Klara and the Sun",
            "category_slug": "bd_comics",
            "isbn13": None,
            "include_keywords": ["Klara and the Sun"],
        }
    )

    bnf = MagicMock(spec=BookMetadataService)
    bnf.resolve_for_alert = AsyncMock(return_value=BookResolution(best=openlibrary_edition))
    bnf.verify_isbn = AsyncMock(return_value=None)

    with patch.object(natural_add_module, "client", mock_client), patch(
        "app.services.ai.modules.natural_add.BookMetadataService", return_value=bnf
    ):
        res = await natural_add_module.parse_prompt("klara and the sun max 15€")

    assert res is not None
    assert res.isbn13 == "9782072909207"
    assert res.isbn_note == "Source OpenLibrary (repli BNF)"
    assert res.book_context is not None
    assert res.book_context.publication_year is None


@pytest.mark.asyncio
async def test_natural_add_typed_isbn_wins_when_confirmed():
    """Un ISBN saisi par l'utilisateur prime, dès lors que la BNF confirme l'ouvrage."""
    mock_client = FakeLLM(
        text={
            "name": "La couleur des choses",
            "category_slug": "bd_comics",
            "isbn13": None,
            "include_keywords": ["La couleur des choses"],
            "max_price_cents": 1200,
        }
    )

    # La résolution par titre renvoie l'autre édition du même ouvrage
    bnf = MagicMock(spec=BookMetadataService)
    bnf.resolve_for_alert = AsyncMock(
        return_value=BookResolution(best=PANCHAUD_ALTERNATIVE)
    )
    bnf.verify_isbn = AsyncMock(return_value=PANCHAUD_EDITION)

    with patch.object(natural_add_module, "client", mock_client), patch(
        "app.services.ai.modules.natural_add.BookMetadataService", return_value=bnf
    ):
        res = await natural_add_module.parse_prompt("9782369903086 max 12€")

    assert res is not None
    assert res.isbn13 == "9782369903086"
    bnf.verify_isbn.assert_awaited_once_with("9782369903086", "La couleur des choses")


@pytest.mark.asyncio
async def test_natural_add_skips_redundant_verification_of_resolved_isbn():
    """L'ISBN déjà issu de la notice BNF n'est pas réinterrogé inutilement."""
    mock_client = FakeLLM(
        text={
            "name": "La couleur des choses",
            "category_slug": "bd_comics",
            "isbn13": None,
            "include_keywords": ["La couleur des choses"],
        }
    )

    bnf = MagicMock(spec=BookMetadataService)
    bnf.resolve_for_alert = AsyncMock(return_value=BookResolution(best=PANCHAUD_EDITION))
    bnf.verify_isbn = AsyncMock(return_value=None)

    with patch.object(natural_add_module, "client", mock_client), patch(
        "app.services.ai.modules.natural_add.BookMetadataService", return_value=bnf
    ):
        res = await natural_add_module.parse_prompt("9782369903086 max 12€")

    assert res is not None
    assert res.isbn13 == "9782369903086"
    bnf.verify_isbn.assert_not_awaited()


@pytest.mark.asyncio
async def test_natural_add_without_verifiable_isbn_falls_back_to_title():
    """Sans confirmation BNF, l'alerte part sur le titre et l'utilisateur est prévenu."""
    mock_client = FakeLLM(
        text={
            "name": "Oeuvre introuvable",
            "category_slug": "bd_comics",
            "isbn13": "9782369903062",
            "include_keywords": ["Oeuvre introuvable"],
        }
    )

    bnf = MagicMock(spec=BookMetadataService)
    bnf.resolve_for_alert = AsyncMock(return_value=None)
    bnf.verify_isbn = AsyncMock(return_value=None)

    with patch.object(natural_add_module, "client", mock_client), patch(
        "app.services.ai.modules.natural_add.BookMetadataService", return_value=bnf
    ):
        res = await natural_add_module.parse_prompt("Oeuvre introuvable")

    assert res is not None
    assert res.isbn13 is None
    assert res.book_context is None
    assert res.isbn_note is not None
    assert "BNF" in res.isbn_note


@pytest.mark.asyncio
async def test_natural_add_drops_ai_isbn_with_invalid_check_digit():
    """Un ISBN au checksum invalide n'est même pas interrogé auprès de la BNF."""
    mock_client = FakeLLM(
        text={
            "name": "One Piece",
            "category_slug": "bd_comics",
            "isbn13": "9782365770000",
            "include_keywords": ["One Piece"],
        }
    )

    bnf = MagicMock(spec=BookMetadataService)
    bnf.resolve_for_alert = AsyncMock(return_value=None)
    bnf.verify_isbn = AsyncMock(return_value=None)

    with patch.object(natural_add_module, "client", mock_client), patch(
        "app.services.ai.modules.natural_add.BookMetadataService", return_value=bnf
    ):
        res = await natural_add_module.parse_prompt("One Piece")

    assert res is not None
    bnf.verify_isbn.assert_not_awaited()
    assert res.isbn13 is None


@pytest.mark.asyncio
async def test_natural_add_skips_isbn_for_series_and_non_book_categories():
    """Série multi-tomes ou produit non littéraire : aucun appel ISBN, aucune latence BNF."""
    mock_client = FakeLLM(
        text={
            "name": "One Piece",
            "category_slug": "bd_comics",
            "isbn13": "9782369903062",
            "is_series": True,
            "include_keywords": ["One Piece"],
        }
    )

    bnf = MagicMock(spec=BookMetadataService)
    bnf.resolve_for_alert = AsyncMock()
    bnf.verify_isbn = AsyncMock()

    with patch.object(natural_add_module, "client", mock_client), patch(
        "app.services.ai.modules.natural_add.BookMetadataService", return_value=bnf
    ):
        res = await natural_add_module.parse_prompt("tous les tomes de One Piece")

    assert res is not None
    assert res.isbn13 is None
    bnf.resolve_for_alert.assert_not_awaited()
    bnf.verify_isbn.assert_not_awaited()


@pytest.mark.asyncio
async def test_keyword_variants_are_grounded_on_book_context():
    """Le contexte BNF réel est transmis au modèle et les exclusions sont nettoyées."""
    from app.services.ai.modules.keyword_variants import keyword_variants_module
    from app.services.ai.schemas import BookContext

    context = BookContext(
        title="La couleur des choses",
        author="Martin Panchaud",
        publisher="Çà et là",
        publication_year=2022,
        format_detail="1 vol. (224 p.) : ill. en coul. ; 24 cm",
        isbn13="9782369903086",
    )
    mock_client = FakeLLM(
        text={
            "variants": ["La couleur des choses", "couleur des choses"],
            "exclusions": [
                "Keeping two",
                "couleur des choses",
                "roman poche",
                "ROMAN POCHE",
                "exemplaire de bibliothèque",
                "a",  # trop court, ignoré par la normalisation
                "film",
                "bande dessinée",
            ],
            "rationale": "Confusions avec l'autre roman du même éditeur.",
        }
    )

    with patch.object(keyword_variants_module, "client", mock_client):
        variants, exclusions = await keyword_variants_module.generate_variants_and_exclusions(
            "La couleur des choses", category_slug="bd_comics", context=context
        )

    # Le prompt reçoit les faits vérifiés, pas une liste de goodies toute faite
    user_prompt = mock_client.calls[0]["user_prompt"]
    assert "9782369903086" in user_prompt
    assert "Martin Panchaud" in user_prompt
    assert "24 cm" in user_prompt
    system_prompt = mock_client.calls[0]["system_prompt"]
    assert "figurine" not in system_prompt

    # Un mot-clé contenu dans le titre bloquerait l'annonce cible : il est retiré
    assert "couleur des choses" not in variants
    assert "couleur des choses" not in exclusions
    # Dédoublonnage sur la casse et plafond d'exclusions
    assert "roman poche" in exclusions
    assert exclusions.count("roman poche") == 1
    assert "film" in exclusions
    assert len(exclusions) <= 6


@pytest.mark.asyncio
async def test_keyword_variants_module_variants_and_exclusions():
    """Vérifie la génération simultanée de variantes et exclusions intelligentes."""
    from app.services.ai.modules.keyword_variants import keyword_variants_module

    mock_client = FakeLLM(
        text={
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
    mock_client = FakeLLM(enabled=False)

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
    assert registry.get_module("profit_scoring") is not None
    assert registry.get_module("natural_add") is not None
    assert registry.get_module("negotiation") is not None

    active_posts = registry.get_active_post_alert_modules()
    assert len(active_posts) >= 1
    assert "profit_scoring" in [m.name for m in active_posts]

    registry.unregister("profit_scoring")
    assert registry.get_module("profit_scoring") is None


def test_ai_payload_schemas_accept_null_lists():
    """Vérifie que les schémas IA acceptent null pour les listes sans lever ValidationError."""
    from app.services.ai.schemas import (
        AutoTuningPayload,
        KeywordVariantsPayload,
        NaturalAddPayload,
    )

    p1 = NaturalAddPayload.model_validate({
        "name": "Tintin",
        "category_slug": "bd_comics",
        "exclude_keywords": None,
        "include_keywords": None,
        "conditions": None,
    })
    assert p1.exclude_keywords is None
    assert p1.name == "Tintin"

    p2 = KeywordVariantsPayload.model_validate({
        "variants": None,
        "exclusions": None,
    })
    assert p2.variants is None
    assert p2.exclusions is None

    p3 = AutoTuningPayload.model_validate({
        "suggested_exclusions": None,
        "explanation": "test",
    })
    assert p3.suggested_exclusions is None


@pytest.mark.asyncio
async def test_natural_add_exposes_competing_editions():
    """Le parsing /add expose les éditions concurrentes pour le clavier du récap."""
    from app.services.ai.modules.natural_add import natural_add_module

    mock_client = FakeLLM(
        text={"name": "la couleur des choses", "category_slug": "bd_comics"}
    )

    bnf = AsyncMock()
    bnf.resolve_for_alert = AsyncMock(
        return_value=BookResolution(
            best=PANCHAUD_EDITION, alternatives=[PANCHAUD_ALTERNATIVE]
        )
    )
    bnf.verify_isbn = AsyncMock(return_value=None)

    with patch.object(natural_add_module, "client", mock_client), patch(
        "app.services.ai.modules.natural_add.BookMetadataService", return_value=bnf
    ):
        res = await natural_add_module.parse_prompt("la couleur des choses. Max 12 €.")

    assert res is not None
    assert [edition.isbn13 for edition in res.edition_alternatives] == [
        PANCHAUD_EDITION.isbn13,
        PANCHAUD_ALTERNATIVE.isbn13,
    ]
    assert res.edition_alternatives[1].publisher == PANCHAUD_ALTERNATIVE.publisher


@pytest.mark.asyncio
async def test_natural_add_without_alternatives_leaves_empty_list():
    """Une œuvre sans édition concurrente ne produit aucun choix."""
    from app.services.ai.modules.natural_add import natural_add_module

    mock_client = FakeLLM(
        text={"name": "la couleur des choses", "category_slug": "bd_comics"}
    )

    bnf = AsyncMock()
    bnf.resolve_for_alert = AsyncMock(return_value=BookResolution(best=PANCHAUD_EDITION))
    bnf.verify_isbn = AsyncMock(return_value=None)

    with patch.object(natural_add_module, "client", mock_client), patch(
        "app.services.ai.modules.natural_add.BookMetadataService", return_value=bnf
    ):
        res = await natural_add_module.parse_prompt("la couleur des choses. Max 12 €.")

    assert res is not None
    assert len(res.edition_alternatives) == 1

