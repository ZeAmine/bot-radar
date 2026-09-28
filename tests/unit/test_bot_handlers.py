"""Tests unitaires pour les handlers du Bot Telegram (/start, /help, /categories)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bot.handlers.categories import CATEGORIES_TEXT, handle_categories
from app.bot.handlers.help import HELP_TEXT, handle_help
from app.bot.handlers.start import START_TEXT, handle_start


@pytest.mark.asyncio
async def test_handle_start():
    """Vérifie le message et le clavier retournés par /start."""
    message = MagicMock()
    message.answer = AsyncMock()

    await handle_start(message)

    message.answer.assert_called_once()
    args, kwargs = message.answer.call_args
    assert args[0] == START_TEXT
    assert "reply_markup" in kwargs
    # Vérifier la présence du bouton
    buttons = kwargs["reply_markup"].inline_keyboard
    assert len(buttons) == 1
    assert buttons[0][0].text == "➕ Créer une alerte"
    assert buttons[0][0].callback_data == "btn_add_alert"


@pytest.mark.asyncio
async def test_handle_help():
    """Vérifie que /help retourne la liste exhaustive des commandes."""
    message = MagicMock()
    message.answer = AsyncMock()

    await handle_help(message)

    message.answer.assert_called_once_with(HELP_TEXT)
    expected_commands = [
        "/add",
        "/show",
        "/edit",
        "/pause",
        "/resume",
        "/delete",
        "/categories",
        "/stats",
        "/cancel",
        "/help",
    ]
    for cmd in expected_commands:
        assert cmd in HELP_TEXT


@pytest.mark.asyncio
async def test_handle_categories():
    """Vérifie que /categories retourne uniquement les 3 catégories autorisées."""
    message = MagicMock()
    message.answer = AsyncMock()

    await handle_categories(message)

    message.answer.assert_called_once_with(CATEGORIES_TEXT)
    assert "📚 Livres" in CATEGORIES_TEXT
    assert "👕 Vêtements" in CATEGORIES_TEXT
    assert "💻 Électronique" in CATEGORIES_TEXT


@pytest.mark.asyncio
async def test_handle_categories_dynamic_with_emoji():
    """Vérifie que /categories utilise c.emoji et les données de la base
    quand db_session est fournie.
    """
    message = MagicMock()
    message.answer = AsyncMock()
    mock_db = AsyncMock()

    cat1 = MagicMock()
    cat1.emoji = "📚"
    cat1.label = "Bandes Dessinées"
    cat2 = MagicMock()
    cat2.emoji = "💻"
    cat2.label = "Informatique"

    with patch("app.bot.handlers.categories.CategoryRepository") as mock_repo_cls:
        mock_repo = MagicMock()
        mock_repo.get_all_active = AsyncMock(return_value=[cat1, cat2])
        mock_repo_cls.return_value = mock_repo

        await handle_categories(message, db_session=mock_db)

        message.answer.assert_called_once()
        text = message.answer.call_args[0][0]
        assert "📚 Bandes Dessinées" in text
        assert "💻 Informatique" in text



@pytest.mark.asyncio
async def test_handle_show_with_query():
    """Vérifie que /show <query> filtre et affiche l'alerte ciblée."""
    import uuid

    from app.bot.handlers.alerts import show_alerts
    from app.core.constants import AlertStatus
    from app.db.models.alert import Alert
    from app.db.models.category import Category
    from app.db.models.user import User

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    cat = Category(id=uuid.uuid4(), label="BD & Comics", slug="bd_comics", emoji="📚")
    mock_alert = Alert(
        id=uuid.uuid4(),
        user_id=db_user.id,
        category=cat,
        name="Soli Deo Gloria",
        isbn="9791034768974",
        status=AlertStatus.ACTIVE,
        include_keywords=["9791034768974"],
        exclude_keywords=[],
        variants=[],
    )

    message = MagicMock()
    message.text = "/show Soli"
    message.answer = AsyncMock()

    mock_session = AsyncMock()
    mock_repo = MagicMock()
    mock_repo.search_by_user = AsyncMock(return_value=[mock_alert])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.bot.handlers.alerts.AlertRepository", lambda _s: mock_repo)
        await show_alerts(message, db_user, mock_session)

    mock_repo.search_by_user.assert_called_once_with(db_user.id, query="Soli", limit=10)
    message.answer.assert_called_once()
    args, kwargs = message.answer.call_args
    assert "Soli Deo Gloria" in args[0]


@pytest.mark.asyncio
async def test_handle_edit_command_shows_categories():
    """Vérifie que /edit affiche le sélecteur de catégorie avec les totaux."""
    import uuid

    from app.bot.handlers.alerts import handle_edit_command
    from app.db.models.category import Category
    from app.db.models.user import User

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    mock_cat_books = Category(id=uuid.uuid4(), label="Livres", slug="bd_comics", emoji="📚")
    mock_cat_tech = Category(id=uuid.uuid4(), label="Électronique", slug="tech", emoji="💻")

    message = MagicMock()
    message.text = "/edit"
    message.answer = AsyncMock()

    mock_session = AsyncMock()
    mock_alert_repo = MagicMock()
    mock_alert_repo.count_by_user = AsyncMock(return_value=12)
    mock_alert_repo.count_by_user_grouped_by_category = AsyncMock(
        return_value={"bd_comics": 11, "tech": 1}
    )

    mock_cat_repo = MagicMock()
    mock_cat_repo.get_all_active = AsyncMock(return_value=[mock_cat_books, mock_cat_tech])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.bot.handlers.alerts.AlertRepository", lambda _s: mock_alert_repo)
        mp.setattr("app.bot.handlers.alerts.CategoryRepository", lambda _s: mock_cat_repo)
        await handle_edit_command(message, db_user, mock_session)

    message.answer.assert_called_once()
    args, kwargs = message.answer.call_args
    assert "Gestion et modification de vos alertes" in args[0]
    keyboard = kwargs["reply_markup"]
    buttons = keyboard.inline_keyboard
    assert any("📚 Livres (11)" in b[0].text for b in buttons)
    assert any("💻 Électronique (1)" in b[0].text for b in buttons)
    assert any("🌐 Toutes les alertes (12)" in b[0].text for b in buttons)


@pytest.mark.asyncio
async def test_handle_select_edit_category():
    """Vérifie que cliquer sur une catégorie affiche la première alerte filtrée."""
    import uuid

    from app.bot.handlers.alerts import handle_select_edit_category
    from app.core.constants import AlertStatus
    from app.db.models.alert import Alert
    from app.db.models.category import Category
    from app.db.models.user import User

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    cat_tech = Category(id=uuid.uuid4(), label="Électronique", slug="tech", emoji="💻")
    tech_alert = Alert(
        id=uuid.uuid4(),
        user_id=db_user.id,
        category=cat_tech,
        name="Raspberry Pi 4",
        status=AlertStatus.ACTIVE,
        include_keywords=["raspberry", "pi 4"],
        exclude_keywords=[],
        variants=[],
    )

    callback = MagicMock()
    callback.data = "ed_cat:tech:0"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    mock_session = AsyncMock()
    mock_alert_repo = MagicMock()
    mock_alert_repo.count_by_user = AsyncMock(return_value=1)
    mock_alert_repo.list_by_user = AsyncMock(return_value=[tech_alert])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.bot.handlers.alerts.AlertRepository", lambda _s: mock_alert_repo)
        await handle_select_edit_category(callback, db_user, mock_session)

    callback.message.edit_text.assert_called_once()
    args, kwargs = callback.message.edit_text.call_args
    assert "Raspberry Pi 4" in args[0]
    keyboard = kwargs["reply_markup"]
    assert any(btn.text == "📁 Catégories" for row in keyboard.inline_keyboard for btn in row)
    assert any(
        "al_list_cat:tech:0" in btn.callback_data
        for row in keyboard.inline_keyboard
        for btn in row
    )


@pytest.mark.asyncio
async def test_handle_edit_category_menu():
    """Vérifie que cliquer sur le bouton Catégories réaffiche le menu de sélection."""
    import uuid

    from aiogram.types import CallbackQuery

    from app.bot.handlers.alerts import handle_edit_category_menu
    from app.db.models.category import Category
    from app.db.models.user import User

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    mock_cat_books = Category(id=uuid.uuid4(), label="Livres", slug="bd_comics", emoji="📚")

    callback = MagicMock(spec=CallbackQuery)
    callback.data = "ed_cat_menu"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    mock_session = AsyncMock()
    mock_alert_repo = MagicMock()
    mock_alert_repo.count_by_user = AsyncMock(return_value=1)
    mock_alert_repo.count_by_user_grouped_by_category = AsyncMock(return_value={"bd_comics": 1})

    mock_cat_repo = MagicMock()
    mock_cat_repo.get_all_active = AsyncMock(return_value=[mock_cat_books])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.bot.handlers.alerts.AlertRepository", lambda _s: mock_alert_repo)
        mp.setattr("app.bot.handlers.alerts.CategoryRepository", lambda _s: mock_cat_repo)
        await handle_edit_category_menu(callback, db_user, mock_session)

    callback.message.edit_text.assert_called_once()
    args, _ = callback.message.edit_text.call_args
    assert "Gestion et modification de vos alertes" in args[0]


@pytest.mark.asyncio
async def test_paginate_alerts_with_category_slug():
    """Vérifie que la pagination préserve le slug de catégorie."""
    import uuid

    from app.bot.handlers.alerts import paginate_alerts
    from app.core.constants import AlertStatus
    from app.db.models.alert import Alert
    from app.db.models.category import Category
    from app.db.models.user import User

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    cat_clothes = Category(id=uuid.uuid4(), label="Vêtements", slug="clothes", emoji="👕")
    clothes_alert = Alert(
        id=uuid.uuid4(),
        user_id=db_user.id,
        category=cat_clothes,
        name="Veste Patagonia",
        status=AlertStatus.ACTIVE,
        include_keywords=["patagonia"],
        exclude_keywords=[],
        variants=[],
    )

    callback = MagicMock()
    callback.data = "al_page:clothes:1"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    mock_session = AsyncMock()
    mock_alert_repo = MagicMock()
    mock_alert_repo.count_by_user = AsyncMock(return_value=3)
    mock_alert_repo.list_by_user = AsyncMock(return_value=[clothes_alert])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.bot.handlers.alerts.AlertRepository", lambda _s: mock_alert_repo)
        await paginate_alerts(callback, db_user, mock_session)

    mock_alert_repo.count_by_user.assert_called_once_with(db_user.id, category_slug="clothes")
    mock_alert_repo.list_by_user.assert_called_once_with(
        db_user.id, limit=1, offset=1, category_slug="clothes"
    )
    callback.message.edit_text.assert_called_once()
    args, kwargs = callback.message.edit_text.call_args
    assert "Veste Patagonia" in args[0]
    keyboard = kwargs["reply_markup"]
    # Vérifier que le bouton précédent pointe vers al_page:clothes:0
    nav_buttons = keyboard.inline_keyboard[1]
    assert any("al_page:clothes:0" in b.callback_data for b in nav_buttons)


@pytest.mark.asyncio
async def test_handle_cancel_command_with_active_state():
    """Vérifie que /cancel réinitialise l'état FSM actif et confirme l'annulation."""
    from app.bot.handlers.alerts import handle_cancel_command

    message = MagicMock()
    message.answer = AsyncMock()

    state = MagicMock()
    state.get_state = AsyncMock(return_value="AlertCreationState:waiting_for_max_price")
    state.clear = AsyncMock()

    await handle_cancel_command(message, state)

    state.clear.assert_called_once()
    message.answer.assert_called_once()
    args, _ = message.answer.call_args
    assert "Opération annulée avec succès" in args[0]


@pytest.mark.asyncio
async def test_handle_cancel_command_without_state():
    """Vérifie le message d'information si aucune opération n'est en cours."""
    from app.bot.handlers.alerts import handle_cancel_command

    message = MagicMock()
    message.answer = AsyncMock()

    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)
    state.clear = AsyncMock()

    await handle_cancel_command(message, state)

    state.clear.assert_not_called()
    message.answer.assert_called_once()
    args, _ = message.answer.call_args
    assert "Aucune opération en cours" in args[0]


@pytest.mark.asyncio
async def test_list_alerts_without_args_shows_category_menu():
    """Vérifie que /list sans argument affiche le sélecteur de catégorie avec les totaux."""
    import uuid

    from app.bot.handlers.alerts import list_alerts
    from app.db.models.category import Category
    from app.db.models.user import User

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    mock_cat = Category(id=uuid.uuid4(), label="Électronique", slug="tech", emoji="💻")

    message = MagicMock()
    message.text = "/list"
    message.answer = AsyncMock()

    mock_session = AsyncMock()
    mock_alert_repo = MagicMock()
    mock_alert_repo.count_by_user = AsyncMock(return_value=12)
    mock_alert_repo.count_by_user_grouped_by_category = AsyncMock(return_value={"tech": 12})

    mock_cat_repo = MagicMock()
    mock_cat_repo.get_all_active = AsyncMock(return_value=[mock_cat])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.bot.handlers.alerts.AlertRepository", lambda _s: mock_alert_repo)
        mp.setattr("app.bot.handlers.alerts.CategoryRepository", lambda _s: mock_cat_repo)
        await list_alerts(message, db_user, mock_session)

    message.answer.assert_called_once()
    args, kwargs = message.answer.call_args
    assert "Choisissez une catégorie pour afficher vos alertes" in args[0]
    keyboard = kwargs["reply_markup"]
    assert any(
        "al_list_cat:tech:0" in btn.callback_data
        for row in keyboard.inline_keyboard
        for btn in row
    )
    assert any(
        "al_list_cat:all:0" in btn.callback_data
        for row in keyboard.inline_keyboard
        for btn in row
    )


@pytest.mark.asyncio
async def test_list_alerts_with_arg_displays_category_directly():
    """Vérifie que /list tech affiche directement les alertes de la catégorie tech."""
    import uuid

    from app.bot.handlers.alerts import list_alerts
    from app.core.constants import AlertStatus
    from app.db.models.alert import Alert
    from app.db.models.category import Category
    from app.db.models.user import User

    db_user = User(id=uuid.uuid4(), telegram_user_id=123)
    cat_tech = Category(id=uuid.uuid4(), label="Électronique", slug="tech", emoji="💻")
    tech_alert = Alert(
        id=uuid.uuid4(),
        user_id=db_user.id,
        category=cat_tech,
        name="Nintendo Switch Mario Kart",
        isbn=None,
        max_item_price_cents=3000,
        status=AlertStatus.ACTIVE,
        include_keywords=["mario kart"],
        exclude_keywords=[],
        variants=[],
    )

    message = MagicMock()
    message.text = "/list tech"
    message.answer = AsyncMock()

    mock_session = AsyncMock()
    mock_alert_repo = MagicMock()
    mock_alert_repo.count_by_user = AsyncMock(return_value=1)
    mock_alert_repo.list_by_user = AsyncMock(return_value=[tech_alert])

    mock_cat_repo = MagicMock()
    mock_cat_repo.get_by_slug = AsyncMock(return_value=cat_tech)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.bot.handlers.alerts.AlertRepository", lambda _s: mock_alert_repo)
        mp.setattr("app.bot.handlers.alerts.CategoryRepository", lambda _s: mock_cat_repo)
        await list_alerts(message, db_user, mock_session)

    message.answer.assert_called_once()
    args, kwargs = message.answer.call_args
    assert "Vos alertes surveillées — 💻 Électronique" in args[0]
    assert "Nintendo Switch Mario Kart" in args[0]
    assert "sans ISBN" not in args[0]
    keyboard = kwargs["reply_markup"]
    assert any(
        "al_list_cat_menu" in btn.callback_data
        for row in keyboard.inline_keyboard
        for btn in row
    )




