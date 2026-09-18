"""Claviers inline pour la gestion complète des alertes."""

import uuid

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.constants import STANDARD_CONDITIONS, AlertStatus, CategorySlug


def get_conditions_toggle_keyboard(selected_conditions: list[str]) -> InlineKeyboardMarkup:
    """Génère le clavier de sélection multiple des états d'articles."""
    buttons: list[list[InlineKeyboardButton]] = []
    for idx, cond in enumerate(STANDARD_CONDITIONS):
        is_selected = cond in selected_conditions
        icon = "✅" if is_selected else "⬜"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {cond}",
                    callback_data=f"cond_toggle:{idx}",
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(text="✅ Confirmer les états", callback_data="cond_confirm")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_alert_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Retourne le clavier de validation finale de création d'alerte."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Créer l’alerte", callback_data="alert_create_confirm")],
            [
                InlineKeyboardButton(text="✏️ Modifier", callback_data="alert_create_edit"),
                InlineKeyboardButton(text="❌ Annuler", callback_data="alert_create_cancel"),
            ],
        ]
    )


def get_category_selection_keyboard() -> InlineKeyboardMarkup:
    """Retourne le clavier de sélection de catégorie lors de la commande /add."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 Livres", callback_data="add_cat:bd_comics")],
            [InlineKeyboardButton(text="💻 Électronique", callback_data="add_cat:tech")],
            [InlineKeyboardButton(text="🎮 Jeux Vidéo", callback_data="add_cat:gaming")],
            [InlineKeyboardButton(text="👕 Vêtements", callback_data="add_cat:clothes")],
            [InlineKeyboardButton(text="❌ Annuler", callback_data="alert_create_cancel")],
        ]
    )


def _build_category_selection_keyboard(
    categories: list,
    counts: dict[str, int],
    total_count: int,
    callback_prefix: str,
) -> InlineKeyboardMarkup:
    """Générateur mutualisé de clavier de sélection de catégorie."""
    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"{cat.emoji} {cat.label} ({counts.get(cat.slug, 0)})",
                callback_data=f"{callback_prefix}:{cat.slug}:0",
            )
        ]
        for cat in categories
    ]
    buttons.append(
        [
            InlineKeyboardButton(
                text=f"🌐 Toutes les alertes ({total_count})",
                callback_data=f"{callback_prefix}:all:0",
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="❌ Fermer",
                callback_data="al_close",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_edit_category_selection_keyboard(
    categories: list,
    counts: dict[str, int],
    total_count: int,
) -> InlineKeyboardMarkup:
    """Retourne le clavier de sélection de catégorie lors de la commande /edit."""
    return _build_category_selection_keyboard(categories, counts, total_count, "ed_cat")


def get_list_category_selection_keyboard(
    categories: list,
    counts: dict[str, int],
    total_count: int,
) -> InlineKeyboardMarkup:
    """Retourne le clavier de sélection de catégorie pour la commande /list."""
    return _build_category_selection_keyboard(categories, counts, total_count, "al_list_cat")


def get_alert_card_keyboard(
    alert_id: uuid.UUID,
    status: AlertStatus,
    current_page: int,
    total_pages: int,
    category_slug: str = "all",
) -> InlineKeyboardMarkup:
    """Retourne le clavier pour une carte d'alerte individuelle dans /show et /edit."""
    pause_text = "⏸ Pause" if status == AlertStatus.ACTIVE else "▶️ Reprendre"
    pause_action = "pause" if status == AlertStatus.ACTIVE else "resume"

    row_actions = [
        InlineKeyboardButton(text="✏️ Modifier", callback_data=f"al_edit:{alert_id}"),
        InlineKeyboardButton(text=pause_text, callback_data=f"al_{pause_action}:{alert_id}"),
        InlineKeyboardButton(text="🗑 Supprimer", callback_data=f"al_del:{alert_id}"),
    ]

    row_nav: list[InlineKeyboardButton] = []
    page_prefix = (
        f"al_page:{category_slug}" if category_slug and category_slug != "all" else "al_page"
    )
    if current_page > 0:
        row_nav.append(
            InlineKeyboardButton(
                text="← Précédent", callback_data=f"{page_prefix}:{current_page - 1}"
            )
        )
    if current_page < total_pages - 1:
        row_nav.append(
            InlineKeyboardButton(
                text="Suivant →", callback_data=f"{page_prefix}:{current_page + 1}"
            )
        )

    inline_keyboard = [row_actions]
    if row_nav:
        inline_keyboard.append(row_nav)

    list_page = current_page // 10
    inline_keyboard.append(
        [
            InlineKeyboardButton(
                text="📋 Liste", callback_data=f"al_list_cat:{category_slug}:{list_page}"
            ),
            InlineKeyboardButton(text="📁 Catégories", callback_data="ed_cat_menu"),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def get_alert_edit_fields_keyboard(
    alert_id: uuid.UUID,
    category_slug: str | None = None,
) -> InlineKeyboardMarkup:
    """Retourne la liste des champs modifiables adaptée à la catégorie de l'alerte."""
    if category_slug == CategorySlug.BD_COMICS.value:
        fields = [
            ("Titre", "name"),
            ("ISBN", "isbn"),
            ("Prix maximum", "max_price"),
            ("Mots-clés inclus", "include_keywords"),
            ("Exclusions", "exclude_keywords"),
            ("Variantes", "variants"),
            ("États acceptés", "conditions"),
        ]
    elif category_slug in (
        CategorySlug.TECH.value,
        CategorySlug.CLOTHES.value,
        CategorySlug.GAMING.value,
    ):
        if category_slug == CategorySlug.TECH.value:
            name_label = "Nom du produit"
        elif category_slug == CategorySlug.GAMING.value:
            name_label = "Nom du jeu / console"
        else:
            name_label = "Nom du vêtement"
        fields = [
            (name_label, "name"),
            ("Prix maximum", "max_price"),
            ("Mots-clés inclus", "include_keywords"),
            ("Exclusions", "exclude_keywords"),
            ("États acceptés", "conditions"),
        ]
    else:
        fields = [
            ("Nom", "name"),
            ("Prix maximum", "max_price"),
            ("Mots-clés inclus", "include_keywords"),
            ("Exclusions", "exclude_keywords"),
            ("États acceptés", "conditions"),
        ]

    buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"ed_f:{alert_id}:{field}")]
        for label, field in fields
    ]
    buttons.append([InlineKeyboardButton(text="← Retour", callback_data=f"al_view:{alert_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_delete_confirmation_keyboard(alert_id: uuid.UUID) -> InlineKeyboardMarkup:
    """Retourne le clavier de confirmation obligatoire de suppression."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Oui, supprimer",
                    callback_data=f"al_del_confirm:{alert_id}",
                )
            ],
            [InlineKeyboardButton(text="← Annuler", callback_data=f"al_view:{alert_id}")],
        ]
    )


def get_alerts_list_keyboard(
    alerts: list,
    current_page: int,
    total_pages: int,
    category_slug: str = "all",
) -> InlineKeyboardMarkup:
    """Retourne le clavier de sélection et de navigation pour /list."""
    buttons: list[list[InlineKeyboardButton]] = []

    # Boutons d'accès direct numérotés (5 par ligne max)
    row: list[InlineKeyboardButton] = []
    start_num = current_page * 10 + 1
    for i, alert in enumerate(alerts, start=start_num):
        row.append(
            InlineKeyboardButton(
                text=f"🔍 {i}",
                callback_data=f"al_view:{alert.id}",
            )
        )
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Ligne de navigation paginée
    nav_row: list[InlineKeyboardButton] = []
    page_prefix = (
        f"al_list_page:{category_slug}"
        if category_slug and category_slug != "all"
        else "al_list_page:all"
    )
    if current_page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ Précédent", callback_data=f"{page_prefix}:{current_page - 1}"
            )
        )
    if current_page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="Suivant ➡️", callback_data=f"{page_prefix}:{current_page + 1}"
            )
        )
    if nav_row:
        buttons.append(nav_row)

    # Ligne d'actions globales : retour au choix de catégorie ou fermer
    buttons.append(
        [
            InlineKeyboardButton(text="📁 Catégories", callback_data="al_list_cat_menu"),
            InlineKeyboardButton(text="❌ Fermer", callback_data="al_close"),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)
