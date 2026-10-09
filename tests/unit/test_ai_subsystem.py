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
from app.services.book_metadata_service import (
    AUTORITE_OPENLIBRARY,
    BookMetadata,
    BookMetadataService,
    BookResolution,
)
from tests.add_fakes import fake_book_search
from tests.fakes_catalogue import FakeCatalogueClient
from tests.fakes_llm import FakeLLM, FlakyLLM

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
        assert res.include_keywords == ["arabe du futur"]
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
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
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
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
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
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
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
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
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
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
        ):
        res = await natural_add_module.parse_prompt("Oeuvre introuvable")

    assert res is not None
    assert res.isbn13 is None
    assert res.book_context is None
    assert res.isbn_note is not None
    assert "BNF" in res.isbn_note
    # Le modèle a soumis un code, donc l'ouvrage n'est pas « non identifié » : la note
    # d'identité dit ce qui a été tenté, et la carte n'ajoute pas son avertissement.
    assert res.book_unidentified is False


@pytest.mark.asyncio
async def test_natural_add_flags_a_work_no_authority_identifies():
    """Rien n'a répondu : le brouillon porte le fait, pour que la carte puisse le dire.

    Le cas réel — `/add whoogys dailycieux, 30€` — produisait une carte avec un titre et
    rien d'autre : ni auteur, ni éditeur, ni ISBN, sans un mot d'explication. L'absence
    de données ne permet pas de déduire qu'une recherche a eu lieu : le module le dit.
    """
    mock_client = FakeLLM(
        text={
            "name": "Whoogys Dailycieux",
            "category_slug": "bd_comics",
            "is_series": False,
            "include_keywords": ["Whoogys Dailycieux"],
        }
    )

    bnf = MagicMock(spec=BookMetadataService)
    bnf.resolve_for_alert = AsyncMock(return_value=None)
    bnf.verify_isbn = AsyncMock(return_value=None)

    with patch.object(natural_add_module, "client", mock_client), patch(
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
        ):
        res = await natural_add_module.parse_prompt("whoogys dailycieux, 30€")

    assert res is not None
    assert res.book_unidentified is True
    assert res.isbn13 is None
    assert res.author is None
    assert res.publisher is None


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
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
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
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
        ):
        res = await natural_add_module.parse_prompt("tous les tomes de One Piece")

    assert res is not None
    assert res.isbn13 is None
    bnf.resolve_for_alert.assert_not_awaited()
    bnf.verify_isbn.assert_not_awaited()


@pytest.mark.asyncio
async def test_natural_add_detects_the_series_the_notice_reveals():
    """Le moteur n'a pas vu la série : la résolution la révèle, et l'ISBN tombe.

    Forme réelle du repli pour cette œuvre : l'autorité rend deux documents, dont un
    numéroté. La notice retenue — celle dont le titre colle exactement à la saisie — ne
    porte aucun numéro, et le document « Vol. 2 » n'est ni une édition ni un homonyme,
    donc la résolution ne le retenait nulle part. Sans `notice_titles`, la série restait
    invisible et l'alerte partait avec l'ISBN d'un tome, titre et mots-clés compris.
    """
    mock_client = FakeLLM(
        text={
            "name": "Absolute Martian Manhunter",
            "category_slug": "bd_comics",
            "is_series": False,
            "include_keywords": ["Absolute Martian Manhunter"],
        }
    )
    retained = BookMetadata(
        title="Absolute Martian Manhunter",
        author="Deniz Camp",
        publisher="DC Comics",
        isbn13="9781799505204",
        source=AUTORITE_OPENLIBRARY,
    )
    bnf = MagicMock(spec=BookMetadataService)
    bnf.resolve_for_alert = AsyncMock(
        return_value=BookResolution(
            best=retained,
            notice_titles=(
                "Absolute Martian Manhunter",
                "Absolute Martian Manhunter, Vol. 2",
            ),
        )
    )
    bnf.verify_isbn = AsyncMock(return_value=None)

    with patch.object(natural_add_module, "client", mock_client), patch(
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
        ):
        res = await natural_add_module.parse_prompt("Absolute Martian Manhunter")

    assert res is not None
    assert res.is_series is True
    assert res.isbn13 is None
    # Ce serait l'édition d'un tome, pas de la série.
    assert res.edition_alternatives == []
    # La provenance de l'ISBN abandonné ne s'affiche pas sous une alerte sans ISBN.
    assert res.isbn_note is None
    assert res.book_context is not None
    assert res.book_context.is_series is True


@pytest.mark.asyncio
async def test_natural_add_reads_the_series_from_the_real_fallback_resolution():
    """Bout en bout : seul le client HTTP est simulé, la résolution est celle du dépôt.

    Le maillon qui manquait n'était ni la règle de détection ni le modèle, mais la notice
    voisine que la résolution jetait. Ce test traverse la vraie résolution de repli, la
    vraie détection et le vrai module : il ne peut pas passer si l'un des trois reperd la
    preuve.
    """
    mock_client = FakeLLM(
        text={
            "name": "Absolute Martian Manhunter",
            "category_slug": "bd_comics",
            "is_series": False,
            "include_keywords": ["Absolute Martian Manhunter"],
        }
    )
    http = FakeCatalogueClient(
        bnf_empty=True,
        openlibrary_docs=[
            {
                "title": "Absolute Martian Manhunter",
                "author_name": ["Deniz Camp"],
                "publisher": ["DC Comics"],
                "first_publish_year": 2025,
                "isbn": ["9781799505204"],
            },
            {
                "title": "Absolute Martian Manhunter, Vol. 2",
                "author_name": ["Deniz Camp"],
                "publisher": ["DC Comics"],
                "first_publish_year": 2026,
                "isbn": ["9781799510871"],
            },
        ],
    )

    with (
        patch.object(natural_add_module, "client", mock_client),
        patch(
            "app.services.book_metadata_service.get_shared_http_client",
            return_value=http,
        ),
    ):
        res = await natural_add_module.parse_prompt("Absolute Martian Manhunter, 10€")

    assert res is not None
    assert res.is_series is True
    assert res.isbn13 is None
    assert res.name == "Absolute Martian Manhunter"


@pytest.mark.asyncio
async def test_natural_add_keeps_the_isbn_when_a_tome_is_requested():
    """« … Tome 2 » reste un tome : la détection ne s'applique pas."""
    mock_client = FakeLLM(
        text={
            "name": "Absolute Martian Manhunter Tome 2",
            "category_slug": "bd_comics",
            "include_keywords": ["Absolute Martian Manhunter Tome 2"],
        }
    )
    volume_2 = BookMetadata(
        title="Absolute Martian Manhunter Vol. 2",
        author="Deniz Camp",
        publisher="DC Comics",
        isbn13="9781799510871",
    )
    bnf = MagicMock(spec=BookMetadataService)
    bnf.resolve_for_alert = AsyncMock(return_value=BookResolution(best=volume_2))
    bnf.verify_isbn = AsyncMock(return_value=None)

    with patch.object(natural_add_module, "client", mock_client), patch(
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
        ):
        res = await natural_add_module.parse_prompt("Absolute Martian Manhunter Tome 2")

    assert res is not None
    assert res.is_series is False
    assert res.isbn13 == "9781799510871"


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
        outcome = await keyword_variants_module.generate_variants_and_exclusions(
            "La couleur des choses", category_slug="bd_comics", context=context
        )
    variants, exclusions = outcome.variants, outcome.exclusions
    assert outcome.answered is True

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
        outcome = await keyword_variants_module.generate_variants_and_exclusions(
            "L'Arabe du futur", category_slug="bd_comics"
        )
        assert outcome.answered is True
        variants, exclusions = outcome.variants, outcome.exclusions
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
    assert "<b>Prix d'achat TTC :</b>" in html
    assert "<b>Revente estimée :</b> ~55,00 €" in html
    assert "<b>Marge nette :</b> +25,50 €" in html
    assert "<b>Indice pépite :</b> 8,5/10" in html
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
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
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
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
        ):
        res = await natural_add_module.parse_prompt("la couleur des choses. Max 12 €.")

    assert res is not None
    assert len(res.edition_alternatives) == 1



# --- Cas réel : « Les Enfants perdus », homonymie BNF -----------------------------
# La BNF ne connaît que le roman de 1989. Le rattacher à la BD de 2025 affichait son
# ISBN, son auteur et son éditeur comme s'ils étaient ceux de la BD, et les variantes
# calculées ensuite raisonnaient sur le roman.
ENFANTS_PERDUS_ROMAN = BookMetadata(
    title="Les Enfants perdus",
    author="Rosemonde Ahou de Saintange",
    publisher="La Pensée universelle",
    isbn13="9782214080627",
    source="bnf",
    publication_year=1989,
)
ENFANTS_PERDUS_BD = BookMetadata(
    title="Les Enfants perdus",
    author="Nef",
    publisher="Éditions du Chêne",
    isbn13="9782810206087",
    source="bnf",
    publication_year=2025,
)
_HOMONYM_RESOLUTION = BookResolution(
    best=ENFANTS_PERDUS_ROMAN,
    other_works=[ENFANTS_PERDUS_BD],
)


def _homonym_bnf(resolution: BookResolution | None = _HOMONYM_RESOLUTION) -> AsyncMock:
    bnf = AsyncMock()
    bnf.resolve_for_alert = AsyncMock(return_value=resolution)
    bnf.verify_isbn = AsyncMock(return_value=None)
    return bnf


@pytest.mark.asyncio
async def test_natural_add_attaches_nothing_when_the_title_is_a_homonym():
    """Un titre partagé par deux œuvres ne rattache ni ISBN, ni auteur, ni éditeur."""
    mock_client = FakeLLM(
        text={
            "name": "Les Enfants perdus",
            "category_slug": "bd_comics",
            "isbn13": None,
            "include_keywords": ["Les Enfants perdus"],
        }
    )
    bnf = _homonym_bnf()

    with patch.object(natural_add_module, "client", mock_client), patch(
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
        ):
        res = await natural_add_module.parse_prompt("les enfants perdus. Max 12 €")

    assert res is not None
    assert res.isbn13 is None
    assert res.author is None
    assert res.publisher is None
    assert res.book_context is None
    assert res.edition_alternatives == []
    assert res.isbn_note is not None
    assert "⚠️ « Les Enfants perdus » : un autre ouvrage porte ce titre." in res.isbn_note
    # Les deux œuvres sont proposées, l'auteur du roman en premier.
    assert [work.author for work in res.work_choices] == [
        "Rosemonde Ahou de Saintange",
        "Nef",
    ]


@pytest.mark.asyncio
async def test_natural_add_reads_the_author_the_user_typed():
    """« Les Enfants perdus de Nef » donne à la résolution l'auteur qu'il a écrit."""
    mock_client = FakeLLM(
        text={"name": "Les Enfants perdus", "category_slug": "bd_comics"}
    )
    bnf = _homonym_bnf(
        BookResolution(
            best=ENFANTS_PERDUS_BD,
            other_works=[ENFANTS_PERDUS_ROMAN],
            author_hint_match="matched",
        )
    )

    with patch.object(natural_add_module, "client", mock_client), patch(
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
        ):
        res = await natural_add_module.parse_prompt("les enfants perdus de Nef. Max 12 €")

    assert res is not None
    bnf.resolve_for_alert.assert_awaited_once_with("Les Enfants perdus", "Nef")
    assert res.author == "Nef"
    assert res.isbn13 == "9782810206087"


@pytest.mark.asyncio
async def test_natural_add_keeps_a_typed_isbn_on_a_homonymic_title():
    """Un ISBN saisi n'est pas perdu quand la BNF ignore l'ouvrage : il est signalé."""
    mock_client = FakeLLM(
        text={"name": "Les Enfants perdus", "category_slug": "bd_comics"}
    )
    bnf = _homonym_bnf()

    with patch.object(natural_add_module, "client", mock_client), patch(
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
        ):
        res = await natural_add_module.parse_prompt("9782810206087 les enfants perdus")

    assert res is not None
    assert res.isbn13 == "9782810206087"
    assert res.isbn_note is not None
    assert "⚠️ « Les Enfants perdus » : un autre ouvrage porte ce titre." in res.isbn_note
    assert "non confirmé par la BNF" in res.isbn_note
    assert len(res.work_choices) == 2


@pytest.mark.asyncio
async def test_natural_add_bnf_notice_overrules_the_author_guessed_by_the_model():
    """Quand la BNF a répondu, c'est sa notice qui fait autorité sur l'auteur."""
    mock_client = FakeLLM(
        text={
            "name": "La couleur des choses",
            "category_slug": "bd_comics",
            "author": "Keeping two",
            "publisher": "Guy Delcourt",
        }
    )
    bnf = AsyncMock()
    bnf.resolve_for_alert = AsyncMock(return_value=BookResolution(best=PANCHAUD_EDITION))
    bnf.verify_isbn = AsyncMock(return_value=None)

    with patch.object(natural_add_module, "client", mock_client), patch(
            "app.services.ai.modules.natural_add.BookSearchUseCase",
            fake_book_search(bnf),
        ):
        res = await natural_add_module.parse_prompt("la couleur des choses")

    assert res is not None
    assert res.author == PANCHAUD_EDITION.author
    assert res.publisher == PANCHAUD_EDITION.publisher


@pytest.mark.asyncio
async def test_keyword_variants_never_carry_an_isbn_or_the_title_itself():
    """Une variante est une autre écriture du titre, jamais une référence d'ouvrage.

    Le récap affichait « les enfants perdus 9782214080627 » et « les enfants perdus
    1989 » : des codes de l'homonyme de 1989, qui font sonner l'alerte BD sur les
    annonces du roman.
    """
    from app.services.ai.modules.keyword_variants import keyword_variants_module

    mock_client = FakeLLM(
        text={
            "variants": [
                "les enfants perdus 9782214080627",
                "978-2-214-08062-7",
                "les enfants perdus 1989",
                "les enfants perdus la pensée universelle",
                "les enfnats perdus",
                "enfants perdus",
            ],
            "exclusions": ["roman"],
            "rationale": "Confusions sur l'homonyme de 1989.",
        }
    )

    with patch.object(keyword_variants_module, "client", mock_client):
        outcome = await keyword_variants_module.generate_variants_and_exclusions(
            "Les Enfants perdus", category_slug="bd_comics"
        )

    assert outcome.answered is True
    # Seule la faute de frappe survit : elle ne contient ni code ni titre complet.
    assert outcome.variants == ["les enfnats perdus"]
    assert "roman" in outcome.exclusions
    # Le prompt interdit explicitement ce qu'il a produit.
    system_prompt = mock_client.calls[0]["system_prompt"]
    assert "tout ISBN" in system_prompt
    assert "ne répète jamais le titre" in system_prompt


_GOOD_PAYLOAD = {
    "variants": ["couleurdeschoses"],
    "exclusions": ["keeping two"],
    "rationale": "Confusion avec l'autre ouvrage du même éditeur.",
}


@pytest.mark.asyncio
async def test_keyword_variants_retries_once_after_a_timeout():
    """Un délai dépassé est un accident : un second essai évite l'alerte amputée."""
    from app.services.ai.modules.keyword_variants import keyword_variants_module

    client = FlakyLLM(None, _GOOD_PAYLOAD)

    with patch.object(keyword_variants_module, "client", client):
        outcome = await keyword_variants_module.generate_variants_and_exclusions(
            "La couleur des choses", category_slug="bd_comics"
        )

    assert client.calls == 2
    assert outcome.answered is True
    assert outcome.variants == ["couleurdeschoses"]
    assert outcome.exclusions == ["keeping two"]


@pytest.mark.asyncio
async def test_keyword_variants_retries_after_an_unreadable_payload():
    """Un JSON que la validation refuse est une panne, pas une réponse : on réessaie."""
    from app.services.ai.modules.keyword_variants import keyword_variants_module

    # `variants` doit être une liste : la charge utile du premier appel est irrecevable.
    client = FlakyLLM({"variants": 42}, _GOOD_PAYLOAD)

    with patch.object(keyword_variants_module, "client", client):
        outcome = await keyword_variants_module.generate_variants_and_exclusions(
            "La couleur des choses", category_slug="bd_comics"
        )

    assert client.calls == 2
    assert outcome.answered is True
    assert outcome.variants == ["couleurdeschoses"]


@pytest.mark.asyncio
async def test_keyword_variants_gives_up_after_two_failures():
    """Deux échecs : le verdict est « personne n'a répondu », et les essais s'arrêtent.

    La troisième réponse est valide : une relance non bornée la ramasserait au lieu
    de conclure, et le doublement du coût resterait invisible pour l’utilisateur.
    """
    from app.services.ai.modules.keyword_variants import keyword_variants_module

    client = FlakyLLM(None, None, _GOOD_PAYLOAD)

    with patch.object(keyword_variants_module, "client", client):
        outcome = await keyword_variants_module.generate_variants_and_exclusions(
            "La couleur des choses", category_slug="bd_comics"
        )

    assert client.calls == 2, "la relance doit être bornée à un essai de plus"
    assert outcome.answered is False
    assert outcome.is_empty is True


@pytest.mark.asyncio
async def test_keyword_variants_does_not_retry_an_answer_without_variants():
    """Un moteur qui répond sans variante a tranché : le réinterroger serait du gaspillage."""
    from app.services.ai.modules.keyword_variants import keyword_variants_module

    client = FlakyLLM({"variants": [], "exclusions": [], "rationale": "Rien à ajouter."})

    with patch.object(keyword_variants_module, "client", client):
        outcome = await keyword_variants_module.generate_variants_and_exclusions(
            "La couleur des choses", category_slug="bd_comics"
        )

    assert client.calls == 1
    assert outcome.answered is True
    assert outcome.is_empty is True


@pytest.mark.asyncio
async def test_keyword_variants_never_calls_an_unconfigured_engine():
    """Un moteur absent n'est jamais appelé, et la relance ne le rend pas présent."""
    from app.services.ai.modules.keyword_variants import keyword_variants_module

    client = FlakyLLM(_GOOD_PAYLOAD, configured=False)

    with patch.object(keyword_variants_module, "client", client):
        outcome = await keyword_variants_module.generate_variants_and_exclusions(
            "La couleur des choses", category_slug="bd_comics"
        )

    assert client.calls == 0
    assert outcome.answered is False


@pytest.mark.asyncio
async def test_natural_add_book_title_matches_without_generated_author():
    """L'auteur proposé par le moteur ne doit pas empêcher une annonce sans auteur."""
    from app.db.models.alert import Alert
    from app.domain.rules.listing_match import evaluate_listing_against_alert

    client = FakeLLM(text={
        "name": "La couleur des choses",
        "category_slug": "bd_comics",
        "include_keywords": ["La couleur des choses", "Martin Panchaud", "Çà et là"],
    })
    http = FakeCatalogueClient(bnf_empty=True, openlibrary_docs=[])
    with (
        patch("app.core.config.settings.GOOGLE_BOOKS_API_KEY", ""),
        patch.object(natural_add_module, "client", client),
        patch("app.services.book_metadata_service.get_shared_http_client", return_value=http),
    ):
        result = await natural_add_module.parse_prompt("La couleur des choses")
    assert result is not None
    alert = Alert(name=result.name, include_keywords=result.include_keywords, status="active")
    match = evaluate_listing_against_alert(
        alert=alert, listing_category_slug="bd_comics",
        listing_normalized_title="Couleur des choses BD très bon état",
        listing_normalized_description="", listing_condition="Très bon état",
        listing_item_price_cents=1000,
    )
    assert match.is_match
    assert result.include_keywords == ["couleur des choses"]


@pytest.mark.asyncio
async def test_natural_add_keeps_separate_product_identity_terms():
    """Une marque et un modèle peuvent apparaître séparément, sans perdre la version."""
    from app.db.models.alert import Alert
    from app.domain.rules.listing_match import evaluate_listing_against_alert

    client = FakeLLM(text={
        "name": "Raspberry Pi 4", "category_slug": "tech",
        "include_keywords": ["Raspberry Pi", "4", "RASPBERRY PI"],
    })
    with patch.object(natural_add_module, "client", client):
        result = await natural_add_module.parse_prompt("Raspberry Pi 4")
    assert result is not None
    assert result.include_keywords == ["raspberry pi", "4"]
    alert = Alert(name=result.name, include_keywords=result.include_keywords, status="active")
    for title, expected in [("Raspberry Pi modèle 4", True), ("Raspberry Pi modèle 3", False)]:
        match = evaluate_listing_against_alert(
            alert=alert, listing_category_slug="tech", listing_normalized_title=title,
            listing_normalized_description="", listing_condition="Très bon état",
            listing_item_price_cents=3000,
        )
        assert match.is_match is expected
