"""Claviers inline pour la gestion complète des alertes."""

import uuid
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.labels import BTN_DELETE, CONDITION_SHORT
from app.core.constants import STANDARD_CONDITIONS, AlertStatus, CategorySlug
from app.db.models.alert import Alert
from app.db.models.category import Category


def get_conditions_toggle_keyboard(selected_conditions: list[str]) -> InlineKeyboardMarkup:
    """Génère le clavier de sélection multiple des états d'articles."""
    buttons: list[list[InlineKeyboardButton]] = []
    for idx, cond in enumerate(STANDARD_CONDITIONS):
        is_selected = cond in selected_conditions
        icon = "✅" if is_selected else "⬜"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {CONDITION_SHORT.get(cond, cond)}",
                    callback_data=f"cond_toggle:{idx}",
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(text="✅ Confirmer les états", callback_data="cond_confirm")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# Marqueurs d'édition. Le même numéro indexe la ligne « Édition » du récap et
# le bouton qui la bascule : c'est ce qui rend le choix lisible sans seconde légende.
EDITION_ORDINALS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣")


def edition_ordinal(index: int) -> str:
    """Numéro d'édition affiché (1️⃣…9️⃣), au-delà un simple ordinal."""
    if 0 <= index < len(EDITION_ORDINALS):
        return EDITION_ORDINALS[index]
    return f"{index + 1}."


def get_alert_confirmation_keyboard(
    show_variants_btn: bool = True,
    edition_alternatives: list[dict[str, Any]] | None = None,
    edition_labels: list[str] | None = None,
    work_choices: list[dict[str, Any]] | None = None,
    work_labels: list[str] | None = None,
) -> InlineKeyboardMarkup:
    """Retourne le clavier de validation finale de création d'alerte.

    Deux choix, dans cet ordre : **quel ouvrage** d'abord, **quelle édition** ensuite.
    Les œuvres homonymes passent avant les éditions parce qu'elles sont la question
    plus large — nommer le mauvais livre ne se rattrape pas en choisissant un autre
    tirage du bon. Elles ne s'affichent qu'à partir de deux œuvres exploitables : sous
    ce seuil, il n'y a rien à trancher.

    Chaque édition concurrente a son bouton, numéroté comme la ligne « Édition »
    du récap (`build_edition_labels`). C'est le dernier moment où l'utilisateur choisit
    l'ISBN qui pilotera le matching, l'URL de recherche Vinted et la cote de marché de
    l'alerte. L'index passé en callback reste celui de `edition_alternatives`, que tous
    les éléments y soient exploitables ou non.

    Les boutons d'édition n'apparaissent qu'à partir de deux éditions exploitables :
    sous ce seuil, le bouton n'opposerait rien à rien, et le récap ne montre déjà pas
    la ligne « Édition ». Le seuil est celui de `format_edition_line`, pour que
    les deux rendus racontent la même chose.
    """
    rows: list[list[InlineKeyboardButton]] = _work_choice_rows(work_choices, work_labels)
    editions = list(edition_alternatives or [])
    labels = edition_labels or []
    has_choice = sum(1 for e in editions if str(e.get("isbn13") or "").strip()) >= 2

    for index, edition in enumerate(editions):
        isbn13 = str(edition.get("isbn13") or "").strip()
        if not has_choice or not isbn13:
            continue
        label = labels[index] if index < len(labels) and labels[index] else (
            f"{edition_ordinal(index)} {isbn13}"
        )
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"al_create_edition:{index}",
            )
        ])

    rows.append(
        [InlineKeyboardButton(text="Créer l'alerte", callback_data="alert_create_confirm")]
    )
    rest = [
        [
            InlineKeyboardButton(text="Modifier", callback_data="alert_create_edit"),
            InlineKeyboardButton(text="Annuler", callback_data="alert_create_cancel"),
        ],
    ]
    if show_variants_btn:
        rest.append(
            [
                InlineKeyboardButton(
                    text="Variantes & Exclusions IA",
                    callback_data="alert_create_ai_variants",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=[*rows, *rest])


def _work_choice_rows(
    work_choices: list[dict[str, Any]] | None,
    work_labels: list[str] | None,
) -> list[list[InlineKeyboardButton]]:
    """Une ligne par œuvre homonyme offered au choix, la plus décrite en premier.

    Le libellé est fourni par le récap (`build_work_labels`) : le clavier ne connaît que
    la forme des données, jamais la mise en forme d'un ouvrage.
    """
    works = list(work_choices or [])
    if sum(1 for work in works if str(work.get("isbn13") or "").strip()) < 2:
        return []

    labels = work_labels or []
    rows: list[list[InlineKeyboardButton]] = []
    for index, work in enumerate(works):
        isbn13 = str(work.get("isbn13") or "").strip()
        if not isbn13:
            continue
        label = labels[index] if index < len(labels) and labels[index] else (
            f"{edition_ordinal(index)} {work.get('author') or isbn13}"
        )
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"al_create_work:{index}")
        ])
    return rows


def get_category_selection_keyboard() -> InlineKeyboardMarkup:
    """Clavier de sélection de catégorie lors de la commande /add : les quatre catégories.

    Sans bouton « Annuler » : à cet instant rien n'est encore engagé, et l'échappatoire
    est `/cancel` (ou le mot « annuler »), que `handle_cancel_command` prend en charge
    depuis n'importe quel état.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 Livres", callback_data="add_cat:bd_comics")],
            [InlineKeyboardButton(text="💻 Électronique", callback_data="add_cat:tech")],
            [InlineKeyboardButton(text="🎮 Jeux Vidéo", callback_data="add_cat:gaming")],
            [InlineKeyboardButton(text="👕 Vêtements", callback_data="add_cat:clothes")],
        ]
    )


def _build_category_selection_keyboard(
    categories: list[Category],
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
                text="Fermer",
                callback_data="al_close",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_edit_category_selection_keyboard(
    categories: list[Category],
    counts: dict[str, int],
    total_count: int,
) -> InlineKeyboardMarkup:
    """Retourne le clavier de sélection de catégorie lors de la commande /edit."""
    return _build_category_selection_keyboard(categories, counts, total_count, "ed_cat")


def get_list_category_selection_keyboard(
    categories: list[Category],
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
    pause_text = "Mettre en pause" if status == AlertStatus.ACTIVE else "Réactiver"
    pause_action = "pause" if status == AlertStatus.ACTIVE else "resume"

    row_actions = [
        InlineKeyboardButton(text="Modifier", callback_data=f"al_edit:{alert_id}"),
        InlineKeyboardButton(text=pause_text, callback_data=f"al_{pause_action}:{alert_id}"),
        InlineKeyboardButton(text=BTN_DELETE, callback_data=f"al_del:{alert_id}"),
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
                text="Liste des alertes", callback_data=f"al_list_cat:{category_slug}:{list_page}"
            ),
            InlineKeyboardButton(text="Catégories", callback_data="ed_cat_menu"),
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
            ("Mots-clés exclus", "exclude_keywords"),
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
            ("Mots-clés exclus", "exclude_keywords"),
            ("États acceptés", "conditions"),
        ]
    else:
        fields = [
            ("Nom", "name"),
            ("Prix maximum", "max_price"),
            ("Mots-clés inclus", "include_keywords"),
            ("Mots-clés exclus", "exclude_keywords"),
            ("États acceptés", "conditions"),
        ]

    # Ajout des actions intelligentes IA
    fields.append(("Suggérer variantes IA", "ai_variants"))
    fields.append(("Auto-tuning exclusions IA", "ai_autotune"))

    buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"ed_f:{alert_id}:{field}")]
        for label, field in fields
    ]
    buttons.append([InlineKeyboardButton(text="← Retour", callback_data=f"al_view:{alert_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)





def get_edit_conditions_keyboard(
    alert_id: uuid.UUID,
    accepted_conditions: list[str] | None,
) -> InlineKeyboardMarkup:
    """Génère le clavier de modification interactive des états acceptés avec bascule."""
    buttons: list[list[InlineKeyboardButton]] = []
    is_all = accepted_conditions is None or len(accepted_conditions) == 0

    for idx, cond in enumerate(STANDARD_CONDITIONS):
        is_selected = is_all or (accepted_conditions is not None and cond in accepted_conditions)
        icon = "✅" if is_selected else "⬜"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {CONDITION_SHORT.get(cond, cond)}",
                    callback_data=f"ed_c_tog:{alert_id}:{idx}",
                )
            ]
        )

    all_icon = "✅" if is_all else "⬜"
    buttons.append(
        [
            InlineKeyboardButton(
                text=f"{all_icon} Tous les états",
                callback_data=f"ed_c_all:{alert_id}",
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="← Retour aux champs",
                callback_data=f"al_edit:{alert_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_cancel_edit_keyboard(alert_id: uuid.UUID) -> InlineKeyboardMarkup:
    """Retourne un clavier avec bouton retour/annuler lors de la saisie d'une nouvelle valeur."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Retour aux champs", callback_data=f"al_edit:{alert_id}")],
        ]
    )


def get_delete_confirmation_keyboard(alert_id: uuid.UUID) -> InlineKeyboardMarkup:
    """Retourne le clavier de confirmation obligatoire de suppression."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Oui, {BTN_DELETE.lower()}",
                    callback_data=f"al_del_confirm:{alert_id}",
                )
            ],
            [InlineKeyboardButton(text="Annuler", callback_data=f"al_view:{alert_id}")],
        ]
    )


def get_alerts_list_keyboard(
    alerts: list[Alert],
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
                text=f"{i}",
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
                text="← Précédent", callback_data=f"{page_prefix}:{current_page - 1}"
            )
        )
    if current_page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="Suivant →", callback_data=f"{page_prefix}:{current_page + 1}"
            )
        )
    if nav_row:
        buttons.append(nav_row)

    # Ligne d'actions globales : retour au choix de catégorie ou fermer
    buttons.append(
        [
            InlineKeyboardButton(text="Catégories", callback_data="al_list_cat_menu"),
            InlineKeyboardButton(text="Fermer", callback_data="al_close"),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)
