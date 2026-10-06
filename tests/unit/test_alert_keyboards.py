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
    """Quatre catégories, et rien d'autre : le bouton « Annuler » n'y est plus.

    L'écran n'engage encore rien, et son échappatoire est `/cancel`. Le clavier rendu
    est inspecté libellé par libellé, pas par la seule présence d'un callback.
    """
    from app.bot.keyboards.alerts import get_category_selection_keyboard

    keyboard = get_category_selection_keyboard()
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert [button.text for button in buttons] == [
        "📚 Livres",
        "💻 Électronique",
        "🎮 Jeux Vidéo",
        "👕 Vêtements",
    ]
    assert [button.callback_data for button in buttons] == [
        "add_cat:bd_comics",
        "add_cat:tech",
        "add_cat:gaming",
        "add_cat:clothes",
    ]
    assert not any(button.callback_data == "alert_create_cancel" for button in buttons)


def test_get_conditions_toggle_keyboard():
    selected = ["Très bon état"]
    keyboard = get_conditions_toggle_keyboard(selected)
    buttons = keyboard.inline_keyboard

    # Doit contenir les conditions + le bouton de confirmation
    assert len(buttons) > 1
    found_checked = False
    for row in buttons[:-1]:
        text = row[0].text
        # Les états sont affichés en abrégé : « TBE » pour « Très bon état ».
        if text.endswith("TBE"):
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

    # rangées : 5 boutons, puis 2, puis la navigation (« Suivant »), puis les actions.
    assert len(rows) == 4
    assert len(rows[0]) == 5
    assert len(rows[1]) == 2
    assert rows[2][0].text == "Suivant"
    assert rows[2][0].callback_data == "al_list_page:tech:1"
    assert rows[3][0].text == "Catégories"
    assert rows[3][0].callback_data == "al_list_cat_menu"
    assert rows[3][1].text == "Fermer"
    assert rows[3][1].callback_data == "al_close"


def test_get_alert_confirmation_keyboard_lists_competing_editions():
    """Chaque édition concurrente a son bouton numéroté, aligné sur la ligne du récap."""
    editions = [
        {"isbn13": "9782369903086", "publisher": "Çà et là"},
        {"isbn13": "9782369904151", "publisher": "Futuropolis"},
    ]
    keyboard = get_alert_confirmation_keyboard(
        edition_alternatives=editions,
        edition_labels=["1️⃣ 2022 · 224 p. · 24 cm", "2️⃣ 2018 · 208 p. · 19 cm"],
    )
    rows = keyboard.inline_keyboard

    # Deux lignes d'éditions, placées avant la validation
    assert rows[0][0].text == "1️⃣ 2022 · 224 p. · 24 cm"
    assert rows[0][0].callback_data == "al_create_edition:0"
    assert rows[1][0].text == "2️⃣ 2018 · 208 p. · 19 cm"
    assert rows[1][0].callback_data == "al_create_edition:1"

    create_row = [button for row in rows for button in row]
    callbacks = [button.callback_data for button in create_row]
    assert "alert_create_confirm" in callbacks
    assert "alert_create_edit" in callbacks
    assert "alert_create_cancel" in callbacks


def test_get_alert_confirmation_keyboard_falls_back_to_isbn_without_label():
    """Sans libellé fourni, le bouton reste exploitable : numéro + ISBN."""
    keyboard = get_alert_confirmation_keyboard(
        edition_alternatives=[
            {"isbn13": "9782369903086"},
            {"isbn13": "9782369904151"},
            {"isbn13": None},
        ],
    )
    rows = keyboard.inline_keyboard

    assert [rows[0][0].text, rows[1][0].text] == [
        "1️⃣ 9782369903086",
        "2️⃣ 9782369904151",
    ]
    # L'édition sans ISBN n'a pas de bouton : le clavier vide l'index de sa position.
    assert rows[2][0].callback_data == "alert_create_confirm"


def test_get_alert_confirmation_keyboard_hides_edition_when_there_is_no_choice():
    """Une édition isolée n'a pas de bouton : il n'y a rien à choisir.

    Le bouton `1️⃣ les Arènes - 2023` d'une seule édition n'opposerait rien à rien,
    et le récap ne montre déjà pas la ligne « Édition » sous ce seuil
    (`format_edition_line`). Les deux rendus doivent raconter la même chose : pas de
    choix, donc aucun bouton.
    """
    keyboard = get_alert_confirmation_keyboard(
        edition_alternatives=[
            {"isbn13": "9791037509659", "publisher": "les Arènes", "publication_year": 2023},
        ],
        edition_labels=["1️⃣ les Arènes - 2023"],
    )
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert not any(data.startswith("al_create_edition") for data in callbacks)
    assert callbacks[0] == "alert_create_confirm"

    # Même verdict quand la seule édition exploitable cohabite avec une notice sans ISBN.
    mixed = get_alert_confirmation_keyboard(
        edition_alternatives=[
            {"isbn13": "9791037509659", "publisher": "les Arènes"},
            {"isbn13": None, "publisher": "inconnu"},
        ],
        edition_labels=["1️⃣ les Arènes", "2️⃣ inconnu"],
    )
    mixed_callbacks = [
        button.callback_data for row in mixed.inline_keyboard for button in row
    ]

    assert not any(data.startswith("al_create_edition") for data in mixed_callbacks)
    assert mixed_callbacks[0] == "alert_create_confirm"


def test_get_alert_confirmation_keyboard_without_editions_is_unchanged():
    """Sans éditions concurrentes, le clavier ne comporte aucune ligne supplémentaire."""
    keyboard = get_alert_confirmation_keyboard()
    callbacks = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]
    assert not any(data.startswith("al_create_edition") for data in callbacks)
    assert callbacks[0] == "alert_create_confirm"


def test_build_edition_choices_keeps_every_edition_once():
    """L'édition retenue reste sélectionnable, et aucun ISBN n'est dupliqué."""
    from app.bot.handlers.alerts_helpers import build_edition_choices
    from app.services.book_metadata_service import BookMetadata, BookResolution

    best = BookMetadata(title="Blacksad", isbn13="9782205078054", publisher="Delcourt")
    duplicate = BookMetadata(title="Blacksad", isbn13="9782205078054", publisher="Delcourt")
    other = BookMetadata(title="Blacksad", isbn13="9782205078061", publisher="Futuropolis")

    choices = build_edition_choices(
        BookResolution(best=best, alternatives=[duplicate, other])
    )

    assert [choice["isbn13"] for choice in choices] == [
        "9782205078054",
        "9782205078061",
    ]
    assert choices[1]["publisher"] == "Futuropolis"


def test_build_edition_choices_handles_missing_resolution():
    from app.bot.handlers.alerts_helpers import build_edition_choices

    assert build_edition_choices(None) == []


def test_build_edition_labels_names_publisher_then_year():
    """Le libellé d'une édition, c'est son éditeur puis son millésime : ce qui la situe.

    Le format de notice BNF (« ill. en coul. ») n'entre pas dans un bouton : il est
    réservé à la ligne « Édition », où la place ne manque pas.
    """
    from app.bot.handlers.alerts_helpers import build_edition_labels

    labels = build_edition_labels(
        [
            {"isbn13": "9782369903086", "publication_year": 2022,
             "format_detail": "1 vol. (224 p.) : ill. en coul. ; 24 cm", "publisher": "Çà et là"},
            {"isbn13": "9782369904151", "publication_year": 2025,
             "format_detail": "1 vol. (208 p.) ; 19 cm", "publisher": "Futuropolis"},
        ]
    )

    assert labels == [
        "1️⃣ Çà et là - 2022",
        "2️⃣ Futuropolis - 2025",
    ]


def test_edition_labels_drop_the_publisher_formula():
    """« les Éditions de Beauvi… » se lit « Beauvi… » : la formule ne distingue rien.

    Le libellé d'un bouton doit tenir à l'écran ; un nom propre court vaut mieux
    qu'une notice recopiée.
    """
    from app.bot.handlers.alerts_helpers import build_edition_labels

    labels = build_edition_labels(
        [
            {"isbn13": "9782369903086", "publication_year": 2020,
             "publisher": "les Éditions de Beauvillé"},
            {"isbn13": "9782369904151", "publication_year": 2025,
             "publisher": "Dargaud"},
        ]
    )

    assert labels == ["1️⃣ Beauvillé - 2020", "2️⃣ Dargaud - 2025"]


def test_work_labels_keep_the_surname_and_the_year_only():
    """Un homonyme se choisit sur le nom de famille, pas sur une notice complète.

    Même si le créateur arrive encore avec son rôle BNF, le libellé doit se réduire
    au nom : c'est lui qui distingue deux ouvrages de même titre.
    """
    from app.bot.handlers.alerts_helpers import build_work_labels

    works = [
        {"isbn13": "9782214080627", "author": "Rosemonde Ahou de Saintange",
         "publisher": "La Pensée universelle", "publication_year": 1989},
        {"isbn13": "9782810206087", "author": "Nicolas. Auteur du texte Fébule",
         "publisher": "les Éditions de Beauvillé", "publication_year": 2020},
    ]

    assert build_work_labels(works) == ["1️⃣ Saintange · 1989", "2️⃣ Fébule · 2020"]


def test_build_edition_labels_drops_year_when_unknown():
    """Sans millésime connu, l'éditeur seul reste un libellé exploitable."""
    from app.bot.handlers.alerts_helpers import build_edition_labels

    assert build_edition_labels(
        [
            {"isbn13": "9782369903086", "publisher": "Çà et là"},
            {"isbn13": "9782369904151", "publication_year": 2025},
        ]
    ) == ["1️⃣ Çà et là", "2️⃣ 2025"]


def test_build_edition_labels_falls_back_to_isbn_when_undistinguishable():
    """Des détails absents ou identiques ne discriminent rien : l'ISBN prend le relais."""
    from app.bot.handlers.alerts_helpers import build_edition_labels

    assert build_edition_labels(
        [{"isbn13": "9782369903086"}, {"isbn13": "9782369904151"}]
    ) == ["1️⃣ 9782369903086", "2️⃣ 9782369904151"]

    assert build_edition_labels(
        [
            {"isbn13": "9782369903086", "publication_year": 2022},
            {"isbn13": "9782369904151", "publication_year": 2022},
        ]
    ) == ["1️⃣ 9782369903086", "2️⃣ 9782369904151"]


def test_format_edition_line_describes_the_followed_edition():
    """« Millésime · éditeur · format » de l'édition retenue, dans l'ordre de lecture."""
    from app.bot.handlers.alerts_helpers import format_edition_line

    editions = [
        {"isbn13": "9782369903086", "publication_year": 2022, "publisher": "Çà et là",
         "format_detail": "1 vol. (224 p.) : ill. en coul. ; 24 cm"},
        {"isbn13": "9782369904151", "publisher": "Éditions poches",
         "format_detail": "1 vol. (224 p.) ; 19 cm"},
    ]

    assert format_edition_line(editions, "9782369903086") == [
        "<b>Édition :</b> 2022 · Çà et là · 224 p. · 24 cm"
    ]
    assert format_edition_line(editions, "9782369904151") == [
        "<b>Édition :</b> Éditions poches · 224 p. · 19 cm"
    ]
    # Le bruit purement décoratif de la notice ne pollue pas l'affichage
    assert "ill. en coul." not in "".join(
        format_edition_line(editions, "9782369903086")
    )
    # Le format est ici, pas dans le bouton : la ligne complète le clavier.
    assert format_edition_line(editions, "9782369903086") != format_edition_line(
        editions, "9782369904151"
    )


def test_format_edition_line_hidden_without_choice():
    """Sans édition concurrente, la carte n'ajoute pas de ligne « Édition ».

    L'ISBN est déjà affiché en tête de carte : il n'y a rien à recouper.
    """
    from app.bot.handlers.alerts_helpers import format_edition_line

    assert format_edition_line(None, "9782369903086") == []
    assert format_edition_line([{"isbn13": "9782369903086"}], "9782369903086") == []
    assert format_edition_line(
        [{"isbn13": "9782369903086"}, {"isbn13": "9782369904151"}], None
    ) == []


def test_format_edition_line_hidden_when_editor_alone_would_repeat():
    """Un éditeur seul ne dit rien de plus que « 🏢 Éditeur » : la ligne est omise."""
    from app.bot.handlers.alerts_helpers import format_edition_line

    editions = [
        {"isbn13": "9782369903086", "publisher": "Çà et là"},
        {"isbn13": "9782369904151", "publisher": "Futuropolis"},
    ]

    assert format_edition_line(editions, "9782369904151") == []


def test_recap_places_the_edition_line_between_publisher_and_max_price():
    """« Édition » complète l'identité du livre : après « Éditeur », avant les critères.

    Elle n'est plus rejetée en fin de carte : l'utilisateur recoupe l'édition qu'il
    suit avec l'éditeur, avant de lire prix et mots-clés. Le libellé est nu — plus de
    « suivie » ni d'emoji, comme les autres champs de la fiche.
    """
    from app.bot.handlers.alerts_helpers import format_semi_auto_recap

    editions = [
        {"isbn13": "9782369903086", "publication_year": 2022, "publisher": "Çà et là",
         "format_detail": "1 vol. (224 p.) : ill. en coul. ; 24 cm"},
        {"isbn13": "9782369904151", "publisher": "Éditions poches",
         "format_detail": "1 vol. (224 p.) ; 19 cm"},
    ]
    recap = format_semi_auto_recap(
        title="La couleur des choses",
        author="Martin Panchaud",
        publisher="Çà et là",
        isbn="9782369903086",
        max_price_cents=1500,
        include_keywords=["la couleur des choses"],
        exclude_keywords=[],
        edition_alternatives=editions,
    )

    lines = recap.splitlines()
    publisher_index = next(
        i for i, line in enumerate(lines) if line.startswith("<b>Éditeur :</b>")
    )
    edition_index = next(
        i for i, line in enumerate(lines) if line.startswith("<b>Édition :</b>")
    )
    price_index = next(
        i for i, line in enumerate(lines) if line.startswith("<b>Prix maximum :</b>")
    )

    assert publisher_index < edition_index < price_index
    assert "<b>Édition :</b> 2022 · Çà et là · 224 p. · 24 cm" in recap
    assert "suivie" not in recap
    assert "📖" not in recap


def test_get_alert_confirmation_keyboard_offers_homonymous_works_first():
    """Les œuvres homonymes passent avant les éditions : d'abord quel livre, puis lequel.

    Les deux questions ne se valent pas — nommer le mauvais ouvrage ne se rattrape pas
    en choisissant un autre tirage du bon.
    """
    works = [
        {
            "isbn13": "9782214080627",
            "title": "Les Enfants perdus",
            "author": "Rosemonde Ahou de Saintange",
            "publisher": "La Pensée universelle",
            "publication_year": 1989,
        },
        {
            "isbn13": "9782810206087",
            "title": "Les Enfants perdus",
            "author": "Nef",
            "publisher": "Éditions du Chêne",
            "publication_year": 2025,
        },
    ]
    editions = [
        {"isbn13": "9782810206087", "publisher": "Éditions du Chêne", "publication_year": 2025},
        {"isbn13": "9791020401234", "publisher": "Éditions du Chêne", "publication_year": 2025},
    ]

    keyboard = get_alert_confirmation_keyboard(
        edition_alternatives=editions,
        work_choices=works,
        work_labels=[
            "1️⃣ Rosemonde Ahou de Saintange · 1989 · La Pensée universelle",
            "2️⃣ Nef · 2025 · Éditions du Chêne",
        ],
    )

    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callbacks.index("al_create_work:0") < callbacks.index("al_create_edition:0")
    assert callbacks.index("al_create_work:1") < callbacks.index("alert_create_confirm")


def test_get_alert_confirmation_keyboard_needs_two_works_to_ask():
    """Une œuvre unique n'oppose rien à rien : aucun bouton de choix d'œuvre."""
    single = [{"isbn13": "9782810206087", "title": "T", "author": "Nef"}]

    keyboard = get_alert_confirmation_keyboard(work_choices=single, work_labels=["1️⃣ Nef"])
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert not any(data.startswith("al_create_work") for data in callbacks)


def test_work_choice_callbacks_fit_telegram_limits():
    """Telegram refuse un `callback_data` de plus de 64 octets."""
    works = [
        {"isbn13": f"97828102060{index:02d}", "title": "T", "author": "Nef"} for index in range(9)
    ]

    keyboard = get_alert_confirmation_keyboard(work_choices=works)

    for row in keyboard.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode("utf-8")) <= 64
