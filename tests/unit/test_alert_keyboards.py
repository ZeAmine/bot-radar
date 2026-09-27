"""Tests unitaires pour la génération des claviers inline d'alertes."""

import uuid

from app.bot.keyboards.alerts import (
    get_alert_card_keyboard,
    get_alert_confirmation_keyboard,
    get_alert_edit_fields_keyboard,
    get_conditions_toggle_keyboard,
    get_delete_confirmation_keyboard,
    get_edit_category_selection_keyboard,
)
from app.bot.keyboards.categories import get_categories_keyboard
from app.core.constants import AlertStatus


def test_get_categories_keyboard():
    keyboard = get_categories_keyboard()
    buttons = keyboard.inline_keyboard
    assert len(buttons) == 4
    labels = [row[0].text for row in buttons]
    assert any("Livres" in label for label in labels)
    assert any("Vêtements" in label for label in labels)
    assert any("Électronique" in label for label in labels)
    assert any("Jeux Vidéo" in label for label in labels)


def test_get_category_selection_keyboard():
    from app.bot.keyboards.alerts import get_category_selection_keyboard

    keyboard = get_category_selection_keyboard()
    buttons = keyboard.inline_keyboard
    assert len(buttons) == 5
    callbacks = [row[0].callback_data for row in buttons]
    assert "add_cat:bd_comics" in callbacks
    assert "add_cat:tech" in callbacks
    assert "add_cat:gaming" in callbacks
    assert "add_cat:clothes" in callbacks
    assert "alert_create_cancel" in callbacks


def test_get_conditions_toggle_keyboard():
    selected = ["Très bon état"]
    keyboard = get_conditions_toggle_keyboard(selected)
    buttons = keyboard.inline_keyboard

    # Doit contenir les conditions + le bouton de confirmation
    assert len(buttons) > 1
    found_checked = False
    for row in buttons[:-1]:
        text = row[0].text
        if "Très bon état" in text:
            assert text.startswith("✅")
            found_checked = True
        else:
            assert text.startswith("⬜")
    assert found_checked is True
    assert buttons[-1][0].callback_data == "cond_confirm"


def test_get_alert_confirmation_keyboard():
    keyboard = get_alert_confirmation_keyboard()
    buttons = keyboard.inline_keyboard
    assert buttons[0][0].callback_data == "alert_create_confirm"
    assert buttons[1][0].callback_data == "alert_create_edit"
    assert buttons[1][1].callback_data == "alert_create_cancel"


def test_get_edit_category_selection_keyboard():
    from unittest.mock import MagicMock

    mock_cats = [
        MagicMock(slug="bd_comics", label="Livres", emoji="📚"),
        MagicMock(slug="tech", label="Électronique", emoji="💻"),
        MagicMock(slug="clothes", label="Vêtements", emoji="👕"),
    ]
    counts = {"bd_comics": 111, "tech": 1, "clothes": 1}
    keyboard = get_edit_category_selection_keyboard(mock_cats, counts, total_count=113)
    buttons = keyboard.inline_keyboard
    assert len(buttons) == 5
    assert "📚 Livres (111)" in buttons[0][0].text
    assert buttons[0][0].callback_data == "ed_cat:bd_comics:0"
    assert "💻 Électronique (1)" in buttons[1][0].text
    assert buttons[1][0].callback_data == "ed_cat:tech:0"
    assert "👕 Vêtements (1)" in buttons[2][0].text
    assert buttons[2][0].callback_data == "ed_cat:clothes:0"
    assert "🌐 Toutes les alertes (113)" in buttons[3][0].text
    assert buttons[3][0].callback_data == "ed_cat:all:0"
    assert buttons[4][0].callback_data == "al_close"


def test_get_alert_card_keyboard():
    alert_id = uuid.uuid4()
    # Test avec statut ACTIVE et pagination sans filtre spécifique
    keyboard = get_alert_card_keyboard(alert_id, AlertStatus.ACTIVE, current_page=0, total_pages=2)
    rows = keyboard.inline_keyboard
    assert len(rows) == 3
    assert rows[0][0].callback_data == f"al_edit:{alert_id}"
    assert rows[0][1].callback_data == f"al_pause:{alert_id}"
    assert rows[0][2].callback_data == f"al_del:{alert_id}"
    assert rows[1][0].callback_data == "al_page:1"
    assert rows[2][0].callback_data == "al_list_cat:all:0"
    assert rows[2][1].callback_data == "ed_cat_menu"

    # Test avec filtre de catégorie
    keyboard_cat = get_alert_card_keyboard(
        alert_id, AlertStatus.ACTIVE, current_page=1, total_pages=3, category_slug="tech"
    )
    rows_cat = keyboard_cat.inline_keyboard
    assert rows_cat[1][0].callback_data == "al_page:tech:0"
    assert rows_cat[1][1].callback_data == "al_page:tech:2"
    assert rows_cat[2][0].callback_data == "al_list_cat:tech:0"
    assert rows_cat[2][1].callback_data == "ed_cat_menu"


def test_get_alert_edit_fields_keyboard():
    alert_id = uuid.uuid4()

    # BD & Comics : Titre, ISBN, Prix max, Mots-clés, Exclusions, Variantes, États + Retour (8
    # boutons)
    kb_bd = get_alert_edit_fields_keyboard(alert_id, category_slug="bd_comics")
    buttons_bd = kb_bd.inline_keyboard
    callbacks_bd = [b[0].callback_data for b in buttons_bd]
    assert f"ed_f:{alert_id}:name" in callbacks_bd
    assert f"ed_f:{alert_id}:isbn" in callbacks_bd
    assert f"ed_f:{alert_id}:max_price" in callbacks_bd
    assert f"ed_f:{alert_id}:include_keywords" in callbacks_bd
    assert f"ed_f:{alert_id}:exclude_keywords" in callbacks_bd
    assert f"ed_f:{alert_id}:variants" in callbacks_bd
    assert f"ed_f:{alert_id}:conditions" in callbacks_bd
    assert f"ed_f:{alert_id}:url" not in callbacks_bd
    assert buttons_bd[-1][0].callback_data == f"al_view:{alert_id}"

    # Électronique / Tech : PAS de variantes, PAS d'URL, PAS d'ISBN
    kb_tech = get_alert_edit_fields_keyboard(alert_id, category_slug="tech")
    buttons_tech = kb_tech.inline_keyboard
    callbacks_tech = [b[0].callback_data for b in buttons_tech]
    assert f"ed_f:{alert_id}:name" in callbacks_tech
    assert f"ed_f:{alert_id}:max_price" in callbacks_tech
    assert f"ed_f:{alert_id}:include_keywords" in callbacks_tech
    assert f"ed_f:{alert_id}:exclude_keywords" in callbacks_tech
    assert f"ed_f:{alert_id}:conditions" in callbacks_tech
    assert f"ed_f:{alert_id}:variants" not in callbacks_tech
    assert f"ed_f:{alert_id}:url" not in callbacks_tech
    assert f"ed_f:{alert_id}:isbn" not in callbacks_tech
    assert buttons_tech[0][0].text == "Nom du produit"
    assert buttons_tech[-1][0].callback_data == f"al_view:{alert_id}"

    # Vêtements : Nom du vêtement, PAS de variantes, PAS d'URL
    kb_clothes = get_alert_edit_fields_keyboard(alert_id, category_slug="clothes")
    assert kb_clothes.inline_keyboard[0][0].text == "Nom du vêtement"
    assert f"ed_f:{alert_id}:variants" not in [
        b[0].callback_data for b in kb_clothes.inline_keyboard]


def test_get_delete_confirmation_keyboard():
    alert_id = uuid.uuid4()
    keyboard = get_delete_confirmation_keyboard(alert_id)
    buttons = keyboard.inline_keyboard
    assert buttons[0][0].callback_data == f"al_del_confirm:{alert_id}"
    assert buttons[1][0].callback_data == f"al_view:{alert_id}"


def test_get_list_category_selection_keyboard():
    from unittest.mock import MagicMock

    from app.bot.keyboards.alerts import get_list_category_selection_keyboard

    mock_cats = [
        MagicMock(slug="bd_comics", label="Livres", emoji="📚"),
        MagicMock(slug="tech", label="Électronique", emoji="💻"),
        MagicMock(slug="clothes", label="Vêtements", emoji="👕"),
    ]
    counts = {"bd_comics": 112, "tech": 11, "clothes": 1}
    keyboard = get_list_category_selection_keyboard(mock_cats, counts, total_count=124)
    buttons = keyboard.inline_keyboard
    assert len(buttons) == 5
    assert "📚 Livres (112)" in buttons[0][0].text
    assert buttons[0][0].callback_data == "al_list_cat:bd_comics:0"
    assert "💻 Électronique (11)" in buttons[1][0].text
    assert buttons[1][0].callback_data == "al_list_cat:tech:0"
    assert "👕 Vêtements (1)" in buttons[2][0].text
    assert buttons[2][0].callback_data == "al_list_cat:clothes:0"
    assert "🌐 Toutes les alertes (124)" in buttons[3][0].text
    assert buttons[3][0].callback_data == "al_list_cat:all:0"
    assert buttons[4][0].callback_data == "al_close"


def test_get_alerts_list_keyboard():
    from unittest.mock import MagicMock

    from app.bot.keyboards.alerts import get_alerts_list_keyboard

    mock_alerts = [MagicMock(id=uuid.uuid4()) for _ in range(7)]
    keyboard = get_alerts_list_keyboard(
        mock_alerts, current_page=0, total_pages=3, category_slug="tech")
    rows = keyboard.inline_keyboard

    # 5 boutons sur row 0, 2 sur row 1, 1 bouton nav 'Suivant ➡️' sur row 2, 1 row d'actions sur row
    # 3
    assert len(rows) == 4
    assert len(rows[0]) == 5
    assert len(rows[1]) == 2
    assert rows[2][0].text == "Suivant ➡️"
    assert rows[2][0].callback_data == "al_list_page:tech:1"
    assert rows[3][0].text == "📁 Catégories"
    assert rows[3][0].callback_data == "al_list_cat_menu"
    assert rows[3][1].text == "❌ Fermer"
    assert rows[3][1].callback_data == "al_close"
