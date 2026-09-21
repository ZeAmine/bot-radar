"""Tests unitaires pour la création d'alertes multi-catégories (/add)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    """Vérifie que la saisie d'un produit tech configure mots-clés, exclusions et conditions tech."""
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
    """Vérifie que cliquer sur Modifier présente le menu de modification et que ses sous-options fonctionnent."""
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
    prompt_text, kwargs = callback.message.edit_text.call_args[0][0], callback.message.edit_text.call_args[1]
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
