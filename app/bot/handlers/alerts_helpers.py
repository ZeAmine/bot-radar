"""Fonctions d'aide et de formatage mutualisées pour les flux d'alerte Telegram."""

import html
import logging
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.formatter import format_cents_to_euros
from app.bot.keyboards.alerts import get_alert_confirmation_keyboard
from app.bot.states.alert_creation import AlertCreationState
from app.core.constants import (
    CATEGORY_EMOJIS,
    CATEGORY_LABELS,
    DEFAULT_BD_CONDITIONS,
    DEFAULT_CLOTHES_CONDITIONS,
    DEFAULT_CLOTHES_EXCLUSIONS,
    DEFAULT_GAMING_CONDITIONS,
    DEFAULT_GAMING_EXCLUSIONS,
    DEFAULT_TECH_CONDITIONS,
    DEFAULT_TECH_EXCLUSIONS,
    AlertStatus,
    CategorySlug,
)
from app.db.models.alert import Alert
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.category_repo import CategoryRepository
from app.services.alert_service import AlertServiceError, parse_price_to_cents
from app.services.book_metadata_service import (
    BookMetadataService,
    format_edition_details,
)

logger = logging.getLogger(__name__)


def _get_alert_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "AlertRepository", AlertRepository)(session)


def _get_category_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "CategoryRepository", CategoryRepository)(session)


def extract_inline_price_and_clean_query(query_str: str) -> tuple[str, int | None]:
    """Extrait un éventuel prix de fin de saisie sans confondre un numéro de modèle ou de tome.

    Exemples :
    - "deathbringer 50" -> ("deathbringer", 5000)
    - "Raspberry Pi 4" -> ("Raspberry Pi 4", None)
    - "Raspberry Pi 4 50€" -> ("Raspberry Pi 4", 5000)
    - "Raspberry Pi 4 50" -> ("Raspberry Pi 4", 5000)
    """
    clean_query = query_str.strip()
    parts = clean_query.split()
    if len(parts) <= 1:
        return clean_query, None

    last_token = parts[-1]
    has_currency = "€" in last_token or "eur" in last_token.lower()
    has_decimal = "." in last_token or "," in last_token

    # Pour un entier pur, n'interpréter comme prix que si >= 15 (exclut tome 1-10, Pi 4, PS 5...)
    is_plausible_price = False
    if has_currency or has_decimal:
        is_plausible_price = True
    else:
        cleaned_digits = last_token.replace(" ", "")
        if cleaned_digits.isdigit() and int(cleaned_digits) >= 15:
            is_plausible_price = True

    if is_plausible_price:
        try:
            price_cents = parse_price_to_cents(last_token)
            remaining_query = " ".join(parts[:-1]).strip()
            if remaining_query:
                return remaining_query, price_cents
        except AlertServiceError:
            pass

    return clean_query, None


def format_semi_auto_recap(
    title: str,
    author: str | None,
    publisher: str | None,
    isbn: str | None,
    max_price_cents: int | None,
    include_keywords: list[str],
    exclude_keywords: list[str],
    category_slug: str = "bd_comics",
    conditions: list[str] | None = None,
    variants: list[str] | None = None,
    is_series: bool = False,
    isbn_note: str | None = None,
    edition_alternatives: list[dict[str, Any]] | None = None,
) -> str:
    """Formate le récapitulatif clair et sans bruit visuel avant confirmation.

    `isbn_note` signale une provenance d'ISBN douteuse ; un avertissement est affiché
    quand l'alerte part sans ISBN vérifié. `edition_alternatives` liste les éditions
    concurrentes de la même œuvre, l'édition retenue étant marquée.
    """
    price_str = f"{max_price_cents / 100:.2f} €".replace(".", ",") if max_price_cents else "Aucun"
    cat_emoji = CATEGORY_EMOJIS.get(CategorySlug(category_slug), "🎯")
    cat_label = CATEGORY_LABELS.get(CategorySlug(category_slug), "Alerte")

    lines = [
        f"{cat_emoji} <b>Récapitulatif de l'alerte ({cat_label})</b>\n",
        f"📌 <b>Nom :</b> {html.escape(title)}",
    ]
    if is_series:
        lines.append("🏷️ <b>Type :</b> Série multi-tomes (surveillance dynamique par tome)")
    if author:
        lines.append(f"✍️ <b>Auteur :</b> {html.escape(author)}")
    if publisher and not _has_competing_editions(edition_alternatives):
        # Avec plusieurs éditions, chaque ligne du bloc « éditions concurrentes »
        # porte déjà son éditeur : la ligne globale ne ferait que doublon.
        lines.append(f"🏢 <b>Éditeur :</b> {html.escape(publisher)}")
    if isbn:
        lines.append(f"🔢 <b>ISBN :</b> <code>{isbn}</code>")
        if isbn_note:
            lines.append(f"ℹ️ <i>{html.escape(isbn_note)}</i>")
    elif isbn_note:
        lines.append("🔢 <b>ISBN :</b> <i>non confirmé par la BNF</i>")
        lines.append(f"⚠️ <i>{html.escape(isbn_note)}</i>")
    lines.extend(format_edition_lines(edition_alternatives, isbn))

    lines.append(f"🎯 <b>Prix max :</b> {price_str}")
    lines.append(f"🔑 <b>Inclus :</b> {', '.join(include_keywords)}")
    if variants:
        lines.append(f"✨ <b>Variantes :</b> {', '.join(variants)}")
    if exclude_keywords:
        lines.append(f"🚫 <b>Exclus :</b> {', '.join(exclude_keywords)}")

    cond_display = (
        ", ".join(conditions)
        if conditions
        else "Neuf avec étiquette, Neuf sans étiquette, Très bon état"
    )
    lines.extend(
        [
            f"💎 <b>États :</b> {cond_display}\n",
            "Confirmer la création de cette alerte ?",
        ]
    )
    return "\n".join(lines)


def build_edition_choices(resolution: Any) -> list[dict[str, Any]]:
    """Éditions d'une résolution BNF, converties en brouillon stockable dans le FSM."""
    if resolution is None:
        return []
    return [edition.model_dump() for edition in resolution.editions()]


def _has_competing_editions(edition_alternatives: list[dict[str, Any]] | None) -> bool:
    """Vrai si l'œuvre est proposée en plusieurs éditions sélectionnables."""
    return len([e for e in (edition_alternatives or []) if e.get("isbn13")]) >= 2


def format_edition_lines(
    edition_alternatives: list[dict[str, Any]] | None,
    current_isbn: str | None,
) -> list[str]:
    """Bloc « éditions concurrentes » du récap, avec l'édition retenue marquée."""
    editions = [
        edition for edition in (edition_alternatives or []) if edition.get("isbn13")
    ]
    if len(editions) < 2:
        return []

    lines = ["", "🔀 <b>Éditions concurrentes de cette œuvre</b>"]
    for edition in editions:
        isbn13 = str(edition["isbn13"])
        marker = "✅ <b>" if isbn13 == current_isbn else "▫️ "
        details = format_edition_details(
            edition.get("publication_year"),
            edition.get("format_detail"),
            edition.get("publisher"),
        )
        suffix = f" — {html.escape(details)}" if details else ""
        lines.append(f"{marker}<code>{isbn13}</code>{suffix}")
    lines.append(
        "<i>L'ISBN retenu filtre Vinted et la cote de marché : "
        "choisis l'édition à surveiller ci-dessous.</i>"
    )
    return lines


def recap_from_state(data: dict[str, Any]) -> str:
    """Construit le récap de création à partir du brouillon FSM (rendu unique)."""
    return format_semi_auto_recap(
        title=data.get("name", "Alerte"),
        author=data.get("author"),
        publisher=data.get("publisher"),
        isbn=data.get("isbn"),
        max_price_cents=data.get("max_item_price_cents"),
        include_keywords=data.get("include_keywords", []),
        exclude_keywords=data.get("exclude_keywords", []),
        category_slug=data.get("category_slug", CategorySlug.BD_COMICS.value),
        conditions=data.get("selected_conditions"),
        variants=data.get("variants"),
        is_series=data.get("is_series", False),
        isbn_note=data.get("isbn_note"),
        edition_alternatives=data.get("edition_alternatives"),
    )


def creation_keyboard(
    data: dict[str, Any],
    show_variants_btn: bool = True,
) -> InlineKeyboardMarkup:
    """Clavier du récap : éditions concurrentes puis validation."""
    return get_alert_confirmation_keyboard(
        show_variants_btn=show_variants_btn,
        edition_alternatives=data.get("edition_alternatives"),
        current_isbn=data.get("isbn"),
    )


def format_alerts_list_text(
    alerts: list[Alert],
    current_page: int,
    total_count: int,
    category_label: str | None = None,
) -> str:
    """Génère le texte de la vue compacte /list."""
    total_pages = max(1, (total_count + 9) // 10)
    title_suffix = f" — {category_label}" if category_label else ""
    header = (
        f"📋 <b>Vos alertes surveillées{title_suffix}</b> "
        f"(Page {current_page + 1}/{total_pages} — "
        f"{total_count} alerte{'s' if total_count > 1 else ''}) :\n"
    )
    lines = [header]
    start_num = current_page * 10 + 1
    for i, a in enumerate(alerts, start=start_num):
        status_icon = "🟢" if a.status == AlertStatus.ACTIVE else "⏸️"
        cat_icon = (
            f"{a.category.emoji} "
            if getattr(a, "category", None) and getattr(a.category, "emoji", None)
            else ""
        )
        price_str = format_cents_to_euros(a.max_item_price_cents)
        if getattr(a, "isbn", None):
            lines.append(
                f"<b>{i}.</b> {status_icon} {cat_icon}<b>{html.escape(a.name)}</b>\n"
                f"   ↳ ISBN : <code>{a.isbn}</code> • Max : {price_str}"
            )
        else:
            lines.append(
                f"<b>{i}.</b> {status_icon} {cat_icon}<b>{html.escape(a.name)}</b>\n"
                f"   ↳ Max : {price_str}"
            )
    lines.append("\n<i>Cliquez sur un numéro ci-dessous pour ouvrir l'alerte :</i>")
    return "\n".join(lines)


async def _resolve_edition_choices(
    title: str, author_hint: str | None
) -> list[dict[str, Any]]:
    """Liste les éditions concurrentes de l'œuvre pour la saisie manuelle.

    Une seule requête BNF supplémentaire : elle ne fait qu'énumérer ce que
    `resolve_for_alert` a déjà tranché. Un échec réseau ne doit jamais bloquer la
    création de l'alerte, il prive seulement l'utilisateur du choix.
    """
    try:
        resolution = await BookMetadataService().resolve_for_alert(title, author_hint)
    except Exception as exc:
        logger.debug("Éditions concurrentes indisponibles pour « %s » : %s", title, exc)
        return []
    return build_edition_choices(resolution)


async def process_bd_query_lookup(
    event: Message | CallbackQuery,
    state: FSMContext,
    query_str: str,
    db_session: Any,
    db_user: User | None = None,
) -> None:
    """Recherche l'œuvre via BookMetadataService et propose la configuration automatique."""
    target_msg = event if isinstance(event, Message) else event.message
    if not target_msg:
        return

    # Vérifier si un prix inline était fourni en fin de saisie (ex: "titre 50" ou "978... 25")
    clean_query, inline_price_cents = extract_inline_price_and_clean_query(query_str)

    status_msg = await target_msg.answer("🔍 Recherche automatique de l'œuvre...")

    service = BookMetadataService()
    meta = await service.lookup(clean_query)

    if not meta:
        await status_msg.edit_text(
            f"❌ Aucune œuvre trouvée pour « <b>{html.escape(clean_query)}</b> ».\n\n"
            "Tu peux réessayer avec un autre titre ou nom d'auteur :"
        )
        await state.set_state(AlertCreationState.waiting_for_query)
        return

    # GARDE ANTI-DOUBLON : Vérifier si l'article est déjà dans les alertes de l'utilisateur
    if db_user:
        alert_repo = _get_alert_repo(db_session)
        existing = await alert_repo.find_existing(db_user.id, isbn=meta.isbn13, title=meta.title)
        if existing:
            status_emoji = "🟢" if existing.status == AlertStatus.ACTIVE else "⏸️"
            status_text = "Active" if existing.status == AlertStatus.ACTIVE else "En pause"
            price_str = (
                f"{existing.max_item_price_cents / 100:.2f} €".replace(".", ",")
                if existing.max_item_price_cents
                else "Aucun"
            )

            # Cas 1 : L'utilisateur avait déjà spécifié un prix en ligne (ex: /add deathbringer 40)
            if inline_price_cents is not None:
                new_price_str = f"{inline_price_cents / 100:.2f} €".replace(".", ",")
                await state.update_data(
                    existing_alert_id=str(existing.id),
                    new_max_price_cents=inline_price_cents,
                )
                warn_text = (
                    "⚠️ <b>Cette BD est déjà surveillée dans tes alertes !</b>\n\n"
                    f"📚 <b>{html.escape(existing.name)}</b>\n"
                    f"🔢 <b>ISBN :</b> <code>{existing.isbn or meta.isbn13 or 'N/A'}</code>\n"
                    f"🎯 <b>Prix max actuel :</b> {price_str}\n"
                    f"{status_emoji} <b>Statut :</b> {status_text}\n\n"
                    f"💡 <i>Tu as indiqué un prix de <b>{new_price_str}</b>.</i>\n"
                    "Souhaites-tu mettre à jour le prix max de cette alerte existante ?"
                )
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=f"🔄 Mettre à jour à {new_price_str}",
                                callback_data=f"upd_alert_price:{existing.id}:{inline_price_cents}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="❌ Conserver l'existante",
                                callback_data="alert_create_cancel",
                            )
                        ],
                    ]
                )
                await status_msg.edit_text(warn_text, reply_markup=keyboard)
                return

            # Cas 2 : Pas de prix spécifié (ex: /add deathbringer)
            await state.update_data(existing_alert_id=str(existing.id))
            await state.set_state(AlertCreationState.waiting_for_max_price)
            warn_text = (
                "⚠️ <b>Cette BD est déjà surveillée dans tes alertes !</b>\n\n"
                f"📚 <b>{html.escape(existing.name)}</b>\n"
                f"🔢 <b>ISBN :</b> <code>{existing.isbn or meta.isbn13 or 'N/A'}</code>\n"
                f"🎯 <b>Prix max actuel :</b> {price_str}\n"
                f"{status_emoji} <b>Statut :</b> {status_text}\n\n"
                "ℹ️ <i>Cette œuvre est déjà surveillée en continu 24h/24.</i>\n\n"
                "💶 <b>Pour modifier son prix max, entre un nouveau montant "
                "(ou clique sur Annuler) :</b>"
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="❌ Conserver sans modification",
                            callback_data="alert_create_cancel",
                        )
                    ]
                ]
            )
            await status_msg.edit_text(warn_text, reply_markup=keyboard)
            return

    # Catégorie BD & Comics par défaut
    category_repo = _get_category_repo(db_session)
    category = await category_repo.get_by_slug(CategorySlug.BD_COMICS.value)
    cat_id_str = str(category.id) if category else ""

    edition_choices = await _resolve_edition_choices(meta.title, meta.author)

    await state.update_data(
        category_id=cat_id_str,
        category_slug=CategorySlug.BD_COMICS.value,
        name=meta.title,
        isbn=meta.isbn13,
        author=meta.author,
        publisher=meta.publisher,
        include_keywords=meta.recommended_includes,
        exclude_keywords=meta.recommended_excludes,
        variants=meta.recommended_variants,
        selected_conditions=list(DEFAULT_BD_CONDITIONS),
        edition_alternatives=edition_choices,
    )

    if inline_price_cents is not None:
        await state.update_data(max_item_price_cents=inline_price_cents)
        await state.set_state(AlertCreationState.confirm_creation)
        draft = await state.get_data()
        await status_msg.edit_text(
            recap_from_state({**draft, "max_item_price_cents": inline_price_cents}),
            reply_markup=creation_keyboard(draft),
        )
        return

    # Demander le prix max
    await state.set_state(AlertCreationState.waiting_for_max_price)
    prompt = (
        f"📚 <b>{html.escape(meta.title)}</b>\n"
        f"✍️ <b>Auteur :</b> {html.escape(meta.author or 'Non spécifié')}\n"
        f"🏢 <b>Éditeur :</b> {html.escape(meta.publisher or 'Non spécifié')}\n"
        f"🔢 <b>ISBN :</b> <code>{meta.isbn13 or 'Non trouvé'}</code>\n\n"
        f"🔑 <b>Inclus :</b> {', '.join(meta.recommended_includes)}\n"
        f"🚫 <b>Exclus :</b> {', '.join(meta.recommended_excludes)}\n"
        "💎 <b>États :</b> Neuf avec étiquette, Neuf sans étiquette, Très bon état\n\n"
        "💶 <b>Indique ton prix maximum pour cette alerte (en €)</b> <i>(ou /cancel)</i> :"
    )
    await status_msg.edit_text(prompt)


async def process_tech_or_clothes_query(
    event: Message | CallbackQuery,
    state: FSMContext,
    query_str: str,
    category_slug: str,
    db_session: Any,
    db_user: User | None = None,
) -> None:
    """Traite la recherche pour un produit high-tech ou vêtement sans passer par la BNF."""
    target_msg = event.message if isinstance(event, CallbackQuery) else event
    if not target_msg:
        return

    # Vérifier si un prix inline était fourni en fin de saisie (ex: "pi 4 50€" ou "titre 50")
    clean_query, inline_price_cents = extract_inline_price_and_clean_query(query_str)

    cat_slug_enum = CategorySlug(category_slug)
    cat_emoji = CATEGORY_EMOJIS.get(cat_slug_enum, "🎯")
    cat_label = CATEGORY_LABELS.get(cat_slug_enum, "Produit")

    # Garde anti-doublon : Vérifier si ce produit est déjà surveillé
    if db_user:
        alert_repo = _get_alert_repo(db_session)
        existing = await alert_repo.find_existing(db_user.id, title=clean_query)
        if existing:
            status_emoji = "🟢" if existing.status == AlertStatus.ACTIVE else "⏸️"
            status_text = "Active" if existing.status == AlertStatus.ACTIVE else "En pause"
            price_str = (
                f"{existing.max_item_price_cents / 100:.2f} €".replace(".", ",")
                if existing.max_item_price_cents
                else "Aucun"
            )

            # Cas 1 : L'utilisateur a spécifié un nouveau prix inline
            if inline_price_cents is not None:
                new_price_str = f"{inline_price_cents / 100:.2f} €".replace(".", ",")
                await state.update_data(
                    existing_alert_id=str(existing.id),
                    new_max_price_cents=inline_price_cents,
                )
                warn_text = (
                    f"⚠️ <b>Ce produit ({cat_label}) est déjà surveillé dans tes alertes !</b>\n\n"
                    f"{cat_emoji} <b>{html.escape(existing.name)}</b>\n"
                    f"🎯 <b>Prix max actuel :</b> {price_str}\n"
                    f"{status_emoji} <b>Statut :</b> {status_text}\n\n"
                    f"💡 <i>Tu as indiqué un prix de <b>{new_price_str}</b>.</i>\n"
                    "Souhaites-tu mettre à jour le prix max de cette alerte existante ?"
                )
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=f"🔄 Mettre à jour à {new_price_str}",
                                callback_data=f"upd_alert_price:{existing.id}:{inline_price_cents}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="❌ Conserver l'existante",
                                callback_data="alert_create_cancel",
                            )
                        ],
                    ]
                )
                if (
                    isinstance(event, CallbackQuery)
                    and event.message
                    and hasattr(event.message, "edit_text")
                ):
                    await event.message.edit_text(warn_text, reply_markup=keyboard)
                else:
                    await target_msg.answer(warn_text, reply_markup=keyboard)
                return

            # Cas 2 : Pas de prix inline spécifié
            await state.update_data(existing_alert_id=str(existing.id))
            await state.set_state(AlertCreationState.waiting_for_max_price)
            warn_text = (
                f"⚠️ <b>Ce produit ({cat_label}) est déjà surveillé dans tes alertes !</b>\n\n"
                f"{cat_emoji} <b>{html.escape(existing.name)}</b>\n\n"
                f"🎯 <b>Prix max actuel :</b> {price_str}\n"
                f"{status_emoji} <b>Statut :</b> {status_text}\n\n"
                "💶 <b>Pour modifier son prix max, entre un nouveau montant "
                "(ou clique sur Annuler) :</b>"
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="❌ Conserver sans modification",
                            callback_data="alert_create_cancel",
                        )
                    ]
                ]
            )
            if (
                isinstance(event, CallbackQuery)
                and event.message
                and hasattr(event.message, "edit_text")
            ):
                await event.message.edit_text(warn_text, reply_markup=keyboard)
            else:
                await target_msg.answer(warn_text, reply_markup=keyboard)
            return

    # Catégorie en base
    category_repo = _get_category_repo(db_session)
    category = await category_repo.get_by_slug(category_slug)
    cat_id_str = str(category.id) if category else ""

    # Découpage des mots-clés inclus (conserve les mots > 1 char ou les chiffres de versions)
    tokens = [w.strip() for w in clean_query.split() if len(w.strip()) > 1 or w.strip().isdigit()]
    includes = tokens if tokens else [clean_query]

    # Exclusions et conditions adaptées par catégorie
    if cat_slug_enum == CategorySlug.TECH:
        default_excludes = list(DEFAULT_TECH_EXCLUSIONS)
        default_conditions = list(DEFAULT_TECH_CONDITIONS)
    elif cat_slug_enum == CategorySlug.GAMING:
        default_excludes = list(DEFAULT_GAMING_EXCLUSIONS)
        default_conditions = list(DEFAULT_GAMING_CONDITIONS)
    else:
        default_excludes = list(DEFAULT_CLOTHES_EXCLUSIONS)
        default_conditions = list(DEFAULT_CLOTHES_CONDITIONS)

    await state.update_data(
        category_id=cat_id_str,
        category_slug=category_slug,
        name=clean_query,
        isbn=None,
        author=None,
        publisher=None,
        include_keywords=includes,
        exclude_keywords=default_excludes,
        variants=[],
        selected_conditions=default_conditions,
    )

    if inline_price_cents is not None:
        await state.update_data(max_item_price_cents=inline_price_cents)
        await state.set_state(AlertCreationState.confirm_creation)
        draft = await state.get_data()
        recap_text = recap_from_state({**draft, "max_item_price_cents": inline_price_cents})
        keyboard = creation_keyboard(draft)
        if (
            isinstance(event, CallbackQuery)
            and event.message
            and hasattr(event.message, "edit_text")
        ):
            await event.message.edit_text(recap_text, reply_markup=keyboard)
        else:
            await target_msg.answer(recap_text, reply_markup=keyboard)
        return

    # Demander le prix max
    await state.set_state(AlertCreationState.waiting_for_max_price)
    prompt = (
        f"{cat_emoji} <b>{html.escape(clean_query)}</b> ({cat_label})\n\n"
        f"🔑 <b>Inclus :</b> {', '.join(includes)}\n"
        f"🚫 <b>Exclus :</b> {', '.join(default_excludes)}\n"
        f"💎 <b>États :</b> {', '.join(default_conditions)}\n\n"
        "💶 <b>Indique ton prix maximum pour cette alerte (en €)</b> <i>(ou /cancel)</i> :"
    )
    if isinstance(event, CallbackQuery) and event.message and hasattr(event.message, "edit_text"):
        await event.message.edit_text(prompt)
    else:
        await target_msg.answer(prompt)
