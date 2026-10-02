"""Tests unitaires pour la création d'alertes multi-catégories (/add)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from aiogram.types import InlineKeyboardMarkup, Message

from app.bot.handlers.alerts import (
    back_to_recap,
    edit_alert_creation_prompt,
    handle_category_selected,
    handle_query_input,
    prompt_new_price,
    restart_from_scratch,
    save_alert,
    start_add_alert,
)
from app.bot.states.alert_creation import AlertCreationState
from app.core.constants import (
    DEFAULT_GAMING_CONDITIONS,
    DEFAULT_GAMING_EXCLUSIONS,
    DEFAULT_TECH_CONDITIONS,
    DEFAULT_TECH_EXCLUSIONS,
    AlertStatus,
)
from app.db.models.category import Category
from app.db.models.user import User


@pytest.mark.asyncio
async def test_start_add_alert_shows_category_keyboard():
    """Vérifie que /add sans argument propose le clavier de choix de catégorie."""
    message = MagicMock()
    message.text = "/add"
    message.answer = AsyncMock()

    state = AsyncMock()
    state.clear = AsyncMock()
    state.set_state = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    await start_add_alert(message, state, db_session, db_user)

    state.clear.assert_called_once()
    state.set_state.assert_called_once_with(AlertCreationState.waiting_for_category)
    message.answer.assert_called_once()
    args, kwargs = message.answer.call_args
    assert "Choisis la catégorie" in args[0]
    keyboard = kwargs["reply_markup"]
    callbacks = [btn[0].callback_data for btn in keyboard.inline_keyboard]
    assert "add_cat:bd_comics" in callbacks
    assert "add_cat:tech" in callbacks
    assert "add_cat:gaming" in callbacks
    assert "add_cat:clothes" in callbacks


@pytest.mark.asyncio
async def test_handle_category_selected_tech():
    """Vérifie que la sélection 'tech' configure l'état et demande le produit."""
    callback = MagicMock()
    callback.data = "add_cat:tech"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    state_data = {}

    async def mock_update_data(**kwargs):
        state_data.update(kwargs)

    async def mock_get_data():
        return state_data

    state = AsyncMock()
    state.get_data = mock_get_data
    state.update_data = mock_update_data
    state.set_state = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    await handle_category_selected(callback, state, db_session, db_user)

    assert state_data.get("category_slug") == "tech"
    state.set_state.assert_called_once_with(AlertCreationState.waiting_for_query)
    callback.message.edit_text.assert_called_once()
    assert "Électronique" in callback.message.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_handle_category_selected_gaming():
    """Vérifie que la sélection 'gaming' configure l'état et demande le jeu / console."""
    callback = MagicMock()
    callback.data = "add_cat:gaming"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    state_data = {}

    async def mock_update_data(**kwargs):
        state_data.update(kwargs)

    async def mock_get_data():
        return state_data

    state = AsyncMock()
    state.get_data = mock_get_data
    state.update_data = mock_update_data
    state.set_state = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    await handle_category_selected(callback, state, db_session, db_user)

    assert state_data.get("category_slug") == "gaming"
    state.set_state.assert_called_once_with(AlertCreationState.waiting_for_query)
    callback.message.edit_text.assert_called_once()
    assert "Jeux Vidéo" in callback.message.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_gaming_query_input_sets_defaults():
    """Vérifie que la saisie d'un jeu configure mots-clés, exclusions et conditions gaming."""
    message = MagicMock()
    message.text = "Mario Kart 8 Deluxe Switch"
    message.answer = AsyncMock()

    state_data = {"category_slug": "gaming"}

    async def mock_update_data(**kwargs):
        state_data.update(kwargs)

    async def mock_get_data():
        return state_data

    state = AsyncMock()
    state.get_data = mock_get_data
    state.update_data = mock_update_data
    state.set_state = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    cat_gaming = Category(id=uuid.uuid4(), slug="gaming", label="Jeux Vidéo", emoji="🎮")
    mock_cat_repo = MagicMock()
    mock_cat_repo.get_by_slug = AsyncMock(return_value=cat_gaming)

    mock_alert_repo = MagicMock()
    mock_alert_repo.find_existing = AsyncMock(return_value=None)

    with (
        patch("app.bot.handlers.alerts.CategoryRepository", return_value=mock_cat_repo),
        patch("app.bot.handlers.alerts.AlertRepository", return_value=mock_alert_repo),
    ):
        await handle_query_input(message, state, db_session, db_user)

    assert state_data["name"] == "Mario Kart 8 Deluxe Switch"
    assert "mario" in [k.lower() for k in state_data["include_keywords"]]
    assert "kart" in [k.lower() for k in state_data["include_keywords"]]
    assert "8" in [k.lower() for k in state_data["include_keywords"]]
    assert state_data["exclude_keywords"] == DEFAULT_GAMING_EXCLUSIONS
    assert state_data["selected_conditions"] == DEFAULT_GAMING_CONDITIONS
    assert state_data["isbn"] is None
    state.set_state.assert_called_once_with(AlertCreationState.waiting_for_max_price)
    message.answer.assert_called_once()
    assert "Mario Kart 8 Deluxe Switch" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_tech_query_input_sets_defaults():
    """Vérifie que la saisie tech configure mots-clés, exclusions et conditions."""
    message = MagicMock()
    message.text = "Raspberry Pi 4"
    message.answer = AsyncMock()

    state_data = {"category_slug": "tech"}

    async def mock_update_data(**kwargs):
        state_data.update(kwargs)

    async def mock_get_data():
        return state_data

    state = AsyncMock()
    state.get_data = mock_get_data
    state.update_data = mock_update_data
    state.set_state = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    cat_tech = Category(id=uuid.uuid4(), slug="tech", label="Électronique", emoji="💻")
    mock_cat_repo = MagicMock()
    mock_cat_repo.get_by_slug = AsyncMock(return_value=cat_tech)

    mock_alert_repo = MagicMock()
    mock_alert_repo.find_existing = AsyncMock(return_value=None)

    with (
        patch("app.bot.handlers.alerts.CategoryRepository", return_value=mock_cat_repo),
        patch("app.bot.handlers.alerts.AlertRepository", return_value=mock_alert_repo),
    ):
        await handle_query_input(message, state, db_session, db_user)

    assert state_data["name"] == "Raspberry Pi 4"
    assert "raspberry" in [k.lower() for k in state_data["include_keywords"]]
    assert "pi" in [k.lower() for k in state_data["include_keywords"]]
    assert "4" in [k.lower() for k in state_data["include_keywords"]]
    assert state_data["exclude_keywords"] == DEFAULT_TECH_EXCLUSIONS
    assert state_data["selected_conditions"] == DEFAULT_TECH_CONDITIONS
    assert state_data["isbn"] is None
    state.set_state.assert_called_once_with(AlertCreationState.waiting_for_max_price)
    message.answer.assert_called_once()
    assert "Raspberry Pi 4" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_save_alert_tech_creates_alert_without_books():
    """Vérifie que la sauvegarde d'une alerte tech n'appelle pas le catalogue de livres."""
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    cat_tech = Category(id=uuid.uuid4(), slug="tech", label="Électronique", emoji="💻")
    user_id = uuid.uuid4()
    db_user = User(id=user_id, telegram_user_id=123)
    db_session = AsyncMock()
    db_session.commit = AsyncMock()

    state_data = {
        "category_id": str(cat_tech.id),
        "category_slug": "tech",
        "name": "Raspberry Pi 4",
        "isbn": None,
        "include_keywords": ["raspberry", "pi", "4"],
        "exclude_keywords": list(DEFAULT_TECH_EXCLUSIONS),
        "variants": [],
        "max_item_price_cents": 5000,
        "selected_conditions": list(DEFAULT_TECH_CONDITIONS),
    }

    state = AsyncMock()
    state.get_data = AsyncMock(return_value=state_data)
    state.clear = AsyncMock()

    mock_alert = MagicMock()
    mock_alert.id = uuid.uuid4()
    mock_alert.name = "Raspberry Pi 4"
    mock_alert.status = AlertStatus.ACTIVE
    mock_alert.category = cat_tech

    mock_alert_repo = MagicMock()
    mock_alert_repo.create = AsyncMock(return_value=mock_alert)

    mock_cat_repo = MagicMock()
    mock_cat_repo.get_by_id = AsyncMock(return_value=cat_tech)
    mock_cat_repo.get_by_slug = AsyncMock(return_value=cat_tech)

    mock_book_repo = MagicMock()

    with (
        patch("app.bot.handlers.alerts.AlertRepository", return_value=mock_alert_repo),
        patch("app.bot.handlers.alerts.CategoryRepository", return_value=mock_cat_repo),
        patch("app.bot.handlers.alerts.BookRepository", return_value=mock_book_repo),
    ):
        await save_alert(callback, state, db_user, db_session)

    # Vérifications
    mock_alert_repo.create.assert_called_once()
    create_kwargs = mock_alert_repo.create.call_args[1]
    assert create_kwargs["name"] == "Raspberry Pi 4"
    assert create_kwargs["isbn"] is None
    assert create_kwargs["max_item_price_cents"] == 5000
    assert "search_text=Raspberry+Pi+4" in create_kwargs["vinted_search_url"]

    # BookRepository ne doit JAMAIS être appelé pour un produit tech
    mock_book_repo.create.assert_not_called()
    mock_book_repo.link_alert_to_book.assert_not_called()

    # Le message de confirmation doit contenir le récapitulatif sans ISBN
    callback.message.edit_text.assert_called_once()
    conf_text = callback.message.edit_text.call_args[0][0]
    assert "Alerte créée avec succès" in conf_text
    assert "Raspberry Pi 4" in conf_text
    assert "50,00 €" in conf_text


@pytest.mark.asyncio
async def test_edit_alert_creation_prompt_and_sub_options():
    """Vérifie que le menu Modifier présente les options et que les sous-options marchent."""
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    state_data = {
        "name": "Veste Dickies",
        "max_item_price_cents": 1000,
        "category_slug": "clothes",
        "include_keywords": ["Veste", "Dickies"],
        "exclude_keywords": [],
        "selected_conditions": ["Très bon état"],
    }
    state = AsyncMock()
    state.get_data = AsyncMock(return_value=state_data)
    state.set_state = AsyncMock()
    state.clear = AsyncMock()

    # 1. Test du menu Modifier
    await edit_alert_creation_prompt(callback, state)
    callback.answer.assert_called_once()
    callback.message.edit_text.assert_called_once()
    call_args = callback.message.edit_text.call_args
    prompt_text, kwargs = call_args[0][0], call_args[1]
    assert "Modification de l'alerte" in prompt_text
    kb = kwargs["reply_markup"]
    assert "10,00 €" in str(kb.inline_keyboard)
    actions = [btn[0].callback_data for btn in kb.inline_keyboard]
    assert "al_create_edit:name" in actions
    assert "al_create_edit:price" in actions
    assert "al_create_edit:include_keywords" in actions
    assert "al_create_edit:exclude_keywords" in actions
    assert "al_create_edit:variants" in actions
    assert "al_create_edit:conditions" in actions
    assert "al_create_edit:restart" in actions
    assert "al_create_edit:back" in actions

    # 2. Test de la sous-option modification de prix
    callback.answer.reset_mock()
    callback.message.edit_text.reset_mock()
    callback.data = "al_create_edit:price"
    await prompt_new_price(callback, state)
    state.set_state.assert_called_once_with(AlertCreationState.waiting_for_edit_field)
    assert "Prix maximum" in callback.message.edit_text.call_args[0][0]

    # 3. Test de la sous-option retour au récapitulatif
    callback.answer.reset_mock()
    callback.message.edit_text.reset_mock()
    state.set_state.reset_mock()
    await back_to_recap(callback, state)
    state.set_state.assert_called_once_with(AlertCreationState.confirm_creation)
    assert "Veste Dickies" in callback.message.edit_text.call_args[0][0]

    # 4. Test de la sous-option recommencer de zéro
    callback.answer.reset_mock()
    callback.message.edit_text.reset_mock()
    state.set_state.reset_mock()
    await restart_from_scratch(callback, state)
    state.clear.assert_called_once()
    state.set_state.assert_called_once_with(AlertCreationState.waiting_for_category)
    assert "Choisis la catégorie" in callback.message.edit_text.call_args[0][0]


class MockMessage(MagicMock):
    __class__ = Message


@pytest.mark.asyncio
async def test_start_add_alert_natural_add_shows_loading_and_edits_to_recap():
    """Vérifie que /add <prompt> envoie le message d'attente puis l'édite avec le récap."""
    from app.services.ai.schemas import NaturalAddResult

    message = MockMessage()
    message.text = "/add Asterix tome 1 10€"
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()

    loading_message = MagicMock()
    loading_message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=loading_message)

    state = AsyncMock()
    state.clear = AsyncMock()
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    fake_result = NaturalAddResult(
        name="Asterix tome 1",
        category_slug="bd_comics",
        is_series=False,
        isbn13="9782012101333",
        author="Goscinny & Uderzo",
        publisher="Hachette",
        include_keywords=["Asterix", "tome 1"],
        exclude_keywords=["figurine"],
        max_price_cents=1000,
        conditions=["Très bon état"],
        explanation="Astérix le Gaulois tome 1 max 10€",
    )

    with (
        patch(
            "app.services.ai.modules.natural_add.NaturalAddModule.is_enabled",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch(
            "app.bot.handlers.alerts_create.natural_add_module.parse_prompt",
            new_callable=AsyncMock,
            return_value=fake_result,
        ),
    ):
        await start_add_alert(message, state, db_session, db_user)

    message.bot.send_chat_action.assert_awaited_once_with(chat_id=12345, action="typing")
    message.answer.assert_awaited_once()
    assert "L'IA analyse votre demande" in message.answer.call_args[0][0]

    # Le message d'attente progresse en deux fois : le jalon « ouvrage trouvé », puis
    # le récap. Le seul `answer` reste le message d'attente initial.
    assert loading_message.edit_text.await_count == 2
    milestone = loading_message.edit_text.await_args_list[0][0][0]
    assert "🔎 <b>Asterix tome 1</b> identifié" in milestone

    edited_text = loading_message.edit_text.call_args[0][0]
    # Le récap porte l'en-tête du langage naturel, qui décrit ce chemin et lui seul.
    assert edited_text.startswith("🪄 <b>Alerte configurée en langage naturel !</b>")
    assert "🔢 <b>ISBN :</b> <code>9782012101333</code>" in edited_text
    assert "📌 <b>Titre :</b> Asterix tome 1" in edited_text
    assert "🎯 <b>Prix max :</b> 10,00 €" in edited_text
    state.set_state.assert_called_with(AlertCreationState.confirm_creation)


@pytest.mark.asyncio
async def test_start_add_alert_natural_add_merges_grounded_keywords_and_isbn_note():
    """Le récap signale la provenance de l'ISBN et fusionne les mots-clés ancrés."""
    from app.services.ai.schemas import BookContext, NaturalAddResult

    message = MockMessage()
    message.text = "/add la couleur des choses. Max 12 €"
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()

    loading_message = MagicMock()
    loading_message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=loading_message)

    state = AsyncMock()
    state.clear = AsyncMock()
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()

    book_context = BookContext(
        title="La couleur des choses",
        author="Martin Panchaud",
        publisher="Çà et là",
        publication_year=2022,
        format_detail="1 vol. (224 p.) : ill. en coul. ; 24 cm",
        isbn13="9782369903086",
    )
    fake_result = NaturalAddResult(
        name="La couleur des choses",
        category_slug="bd_comics",
        is_series=False,
        isbn13="9782369903086",
        author="Martin Panchaud",
        publisher="Çà et là",
        include_keywords=["La couleur des choses"],
        exclude_keywords=["roman poche"],
        max_price_cents=1200,
        isbn_note="Source OpenLibrary (repli BNF)",
        book_context=book_context,
    )

    keywords_mock = AsyncMock(return_value=(["couleurdeschoses"], ["keeping two"]))

    with (
        patch(
            "app.services.ai.modules.natural_add.NaturalAddModule.is_enabled",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch(
            "app.bot.handlers.alerts_create.natural_add_module.parse_prompt",
            new_callable=AsyncMock,
            return_value=fake_result,
        ),
        patch(
            "app.bot.handlers.alerts_create.keyword_variants_module"
            ".generate_variants_and_exclusions",
            new=keywords_mock,
        ),
    ):
        await start_add_alert(message, state, AsyncMock(), User(id=uuid.uuid4()))

    # La génération de mots-clés reçoit la fiche vérifiée de l'ouvrage
    keywords_mock.assert_awaited_once_with(
        "La couleur des choses",
        category_slug="bd_comics",
        context=book_context,
    )

    edited_text = loading_message.edit_text.call_args[0][0]
    assert "9782369903086" in edited_text
    # La provenance secondaire de l'ISBN reste signalée dans la carte compacte.
    assert "Source OpenLibrary (repli BNF)" in edited_text
    assert "🔢 <b>ISBN :</b> <code>9782369903086</code>" in edited_text
    # Exclusions du parsing puis celles ancrées sur la fiche, sans doublon
    assert "roman poche" in edited_text
    assert "keeping two" in edited_text
    assert "couleurdeschoses" in edited_text

    state_data = state.update_data.call_args.kwargs["data"]
    assert state_data["exclude_keywords"] == ["roman poche", "keeping two"]
    assert state_data["variants"] == ["couleurdeschoses"]
    assert state_data["book_context"]["isbn13"] == "9782369903086"


@pytest.mark.asyncio
async def test_start_add_alert_natural_add_warns_when_isbn_unconfirmed():
    """Sans ISBN confirmé, le récap prévient que la surveillance se fait par titre."""
    from app.services.ai.schemas import NaturalAddResult

    message = MockMessage()
    message.text = "/add oeuvre inconnue"
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()

    loading_message = MagicMock()
    loading_message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=loading_message)

    state = AsyncMock()
    state.clear = AsyncMock()
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()

    fake_result = NaturalAddResult(
        name="Œuvre inconnue",
        category_slug="bd_comics",
        isbn13=None,
        include_keywords=["Œuvre inconnue"],
        isbn_note="Aucun ISBN confirmé par la BNF pour « Œuvre inconnue » : surveillance par titre",
    )

    with (
        patch(
            "app.services.ai.modules.natural_add.NaturalAddModule.is_enabled",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch(
            "app.bot.handlers.alerts_create.natural_add_module.parse_prompt",
            new_callable=AsyncMock,
            return_value=fake_result,
        ),
        patch(
            "app.bot.handlers.alerts_create.keyword_variants_module"
            ".generate_variants_and_exclusions",
            new_callable=AsyncMock,
            return_value=([], []),
        ),
    ):
        await start_add_alert(message, state, AsyncMock(), User(id=uuid.uuid4()))

    edited_text = loading_message.edit_text.call_args[0][0]
    assert "non confirmé par la BNF" in edited_text
    assert "surveillance par titre" in edited_text


@pytest.mark.asyncio
async def test_start_add_alert_natural_add_fallback_on_parse_failure():
    """Vérifie que si l'IA ne peut pas parser, le message d'attente bascule sur le sélecteur."""
    message = MockMessage()
    message.text = "/add requete incomprehensible"
    message.chat.id = 12345
    message.bot = MagicMock()
    message.bot.send_chat_action = AsyncMock()

    loading_message = MagicMock()
    loading_message.edit_text = AsyncMock()
    message.answer = AsyncMock(return_value=loading_message)

    state = AsyncMock()
    state.clear = AsyncMock()
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    db_session = AsyncMock()

    with (
        patch(
            "app.services.ai.modules.natural_add.NaturalAddModule.is_enabled",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch(
            "app.bot.handlers.alerts_create.natural_add_module.parse_prompt",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await start_add_alert(message, state, db_session, db_user)

    message.bot.send_chat_action.assert_awaited_once_with(chat_id=12345, action="typing")
    message.answer.assert_awaited_once()
    loading_message.edit_text.assert_awaited_once()
    edited_text = loading_message.edit_text.call_args[0][0]
    assert "Choisis la catégorie de ce produit" in edited_text
    state.set_state.assert_called_with(AlertCreationState.waiting_for_category)


@pytest.mark.asyncio
async def test_handle_negotiate_shows_toast_and_typing_action():
    """Vérifie que [ 💬 Négocier ] répond immédiatement par un toast et du typing."""
    from app.bot.handlers.callbacks import handle_negotiate
    from app.services.ai.schemas import NegotiationResult

    listing_id = uuid.uuid4()
    callback = MagicMock()
    callback.data = f"negotiate:{listing_id}"
    callback.answer = AsyncMock()
    mock_msg = MockMessage()
    mock_msg.chat = MagicMock()
    mock_msg.chat.id = 12345
    mock_msg.bot = MagicMock()
    mock_msg.bot.send_chat_action = AsyncMock()
    mock_msg.reply = AsyncMock()
    callback.message = mock_msg

    fake_listing = MagicMock()
    fake_listing.id = listing_id
    fake_listing.title = "BD Tintin"
    fake_listing.item_price_cents = 2000

    fake_offer = NegotiationResult(
        suggested_offer_cents=1600,
        suggested_offer_euros=16.0,
        message="Bonjour, seriez-vous ouvert à 16€ ?",
    )

    db_session = AsyncMock()

    with (
        patch("app.bot.handlers.callbacks.ListingRepository") as mock_repo_cls,
        patch(
            "app.bot.handlers.callbacks.negotiation_module.generate_offer",
            new_callable=AsyncMock,
            return_value=fake_offer,
        ),
    ):
        mock_repo = MagicMock()
        mock_repo.get_by_id = AsyncMock(return_value=fake_listing)
        mock_repo_cls.return_value = mock_repo

        await handle_negotiate(callback, db_session)

    callback.answer.assert_awaited_once_with("⏳ Coach IA : Analyse de l'offre en cours...")
    callback.message.bot.send_chat_action.assert_awaited_once_with(chat_id=12345, action="typing")
    callback.message.reply.assert_awaited_once()
    reply_text = callback.message.reply.call_args[1]["text"]
    assert "Offre suggérée" in reply_text


@pytest.mark.asyncio
async def test_select_alert_creation_edition_switches_isbn():
    """Le clic sur une édition concurrente change l'ISBN et rafraîchit le récap."""
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import CallbackQuery, Message

    from app.bot.handlers.alerts_create_confirm import select_alert_creation_edition
    from app.bot.states.alert_creation import AlertCreationState

    editions = [
        {"isbn13": "9782369903086", "publisher": "Çà et là"},
        {"isbn13": "9782369904151", "publisher": "Futuropolis"},
    ]
    draft = {
        "name": "La couleur des choses",
        "author": "Martin Panchaud",
        "publisher": "Çà et là",
        "isbn": "9782369903086",
        "max_item_price_cents": 1200,
        "include_keywords": ["la couleur des choses"],
        "exclude_keywords": [],
        "category_slug": "bd_comics",
        "edition_alternatives": editions,
    }

    stored = dict(draft)
    state = AsyncMock()
    state.get_data = AsyncMock(side_effect=lambda: dict(stored))

    async def _update(data=None, **_kwargs):
        stored.update(data or {})

    state.update_data = AsyncMock(side_effect=_update)
    state.set_state = AsyncMock()

    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.data = "al_create_edition:1"
    callback.message = message
    callback.answer = AsyncMock()

    await select_alert_creation_edition(callback, state)

    assert stored["isbn"] == "9782369904151"
    assert stored["publisher"] == "Futuropolis"
    assert stored["is_series"] is False
    state.set_state.assert_awaited_with(AlertCreationState.confirm_creation)

    text = message.edit_text.call_args[0][0]
    assert "🏢 <b>Éditeur :</b> Futuropolis" in text
    assert "🔢 <b>ISBN :</b> <code>9782369904151</code>" in text
    # Sans millésime ni format, la ligne « Édition suivie » n'aurait rien de plus à dire
    # que « 🏢 Éditeur » : elle est omise plutôt que dupliquée.
    assert "Édition suivie" not in text

    keyboard = message.edit_text.call_args[1]["reply_markup"]
    edition_buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("al_create_edition")
    ]
    assert [button.text for button in edition_buttons] == [
        "1️⃣ Çà et là",
        "2️⃣ Futuropolis",
    ]


@pytest.mark.asyncio
async def test_select_alert_creation_edition_rejects_out_of_range_index():
    """Un index hors limites ne doit jamais modifier le brouillon."""
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import CallbackQuery, Message

    from app.bot.handlers.alerts_create_confirm import select_alert_creation_edition

    draft = {"isbn": "9782369903086", "edition_alternatives": [{"isbn13": "9782369903086"}]}
    state = AsyncMock()
    state.get_data = AsyncMock(return_value=draft)
    state.update_data = AsyncMock()

    callback = MagicMock(spec=CallbackQuery)
    callback.data = "al_create_edition:7"
    callback.message = MagicMock(spec=Message)
    callback.answer = AsyncMock()

    await select_alert_creation_edition(callback, state)

    state.update_data.assert_not_called()
    callback.answer.assert_awaited_once_with("Édition inconnue.", show_alert=True)


@pytest.mark.asyncio
async def test_finalize_loading_message_falls_back_to_plain_text():
    """Un refus de formatage Telegram ne doit pas remonter au webhook.

    Un handler qui lève vaut « update non traité » pour Telegram, qui rejoue alors le
    même update : l'utilisateur voit « Recherche IA en cours » en boucle.
    """
    from aiogram.exceptions import TelegramBadRequest

    from app.bot.handlers.alerts_create import _finalize_loading_message

    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    # Dans l'incident, l'édition du message d'attente échoue puis l'envoi aussi :
    # c'est ce second échec qui faisait remonter l'exception au webhook.
    loading_msg = AsyncMock()
    loading_msg.edit_text = AsyncMock(side_effect=TelegramBadRequest(
        method=None, message="message is not modified"
    ))

    event = AsyncMock()
    event.answer = AsyncMock(
        side_effect=[
            TelegramBadRequest(method=None, message="can't parse entities"),
            None,
        ]
    )

    await _finalize_loading_message(
        loading_msg,
        event,
        "🎯 <b>Récapitulatif</b>\n📌 <b>Nom :</b> ASTON & PS5",
        keyboard,
    )

    assert event.answer.await_count == 2
    final_call = event.answer.await_args_list[1]
    assert "<b>" not in final_call.args[0]
    assert "&amp;" in final_call.args[0] or "&" in final_call.args[0]
    assert final_call.kwargs["parse_mode"] is None


@pytest.mark.asyncio
async def test_finalize_loading_message_does_not_swallow_other_errors():
    """Un échec réseau reste une panne : il doit remonter, pas être masqué en texte brut."""
    from app.bot.handlers.alerts_create import _finalize_loading_message

    loading_msg = AsyncMock()
    loading_msg.edit_text = AsyncMock(side_effect=RuntimeError("non éditable"))
    event = AsyncMock()
    event.answer = AsyncMock(side_effect=ConnectionError("réseau indisponible"))

    with pytest.raises(ConnectionError):
        await _finalize_loading_message(
            loading_msg, event, "texte", InlineKeyboardMarkup(inline_keyboard=[])
        )
