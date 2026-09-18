import html
import logging
import re
import urllib.parse
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.formatter import format_alert_card, format_alert_recap, format_cents_to_euros
from app.bot.keyboards.alerts import (
    get_alert_card_keyboard,
    get_alert_confirmation_keyboard,
    get_alert_edit_fields_keyboard,
    get_alerts_list_keyboard,
    get_category_selection_keyboard,
    get_conditions_toggle_keyboard,
    get_delete_confirmation_keyboard,
    get_edit_category_selection_keyboard,
    get_list_category_selection_keyboard,
)
from app.bot.keyboards.categories import get_categories_keyboard
from app.bot.states.alert_creation import AlertCreationState, AlertEditState
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
    STANDARD_CONDITIONS,
    AlertStatus,
    CategorySlug,
    ComparisonStatus,
    MatchConfidence,
)
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.category_repo import CategoryRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
from app.providers.registry import ProviderRegistry
from app.services.alert_service import (
    AlertServiceError,
    parse_comma_separated_list,
    parse_price_to_cents,
    validate_and_parse_vinted_url,
)
from app.services.book_catalog_service import BookCatalogService, isbn10_to_isbn13
from app.services.book_metadata_service import BookMetadata, BookMetadataService
from app.services.matcher_service import normalize_text

logger = logging.getLogger(__name__)


def extract_clean_isbn(text: str) -> str | None:
    """Extrait et nettoie un ISBN-10 ou ISBN-13 depuis un texte ou une URL."""
    if not text:
        return None
    # 1. Chercher un ISBN-13 (ex: 9782803672080 ou 978-2-8036-7208-0)
    isbn13_match = re.search(
        r"(?:978|979)[-\s]?[0-9][-\s]?[0-9]{2,5}[-\s]?[0-9]{2,5}[-\s]?[0-9]", text
    )
    if isbn13_match:
        cleaned = re.sub(r"[^0-9]", "", isbn13_match.group(0))
        if len(cleaned) == 13:
            return cleaned

    # 2. Chercher un ISBN-10 avec frontières de mots
    isbn10_matches = re.findall(r"\b[0-9]{9}[0-9X]\b", text.upper().replace("-", " "))
    if isbn10_matches:
        cleaned = isbn10_matches[0]
        converted = isbn10_to_isbn13(cleaned)
        return converted or cleaned

    # 3. Repli si la chaîne sans ponctuation fait 13 ou 10 caractères
    cleaned_all = re.sub(r"[^0-9X]", "", text.strip().upper())
    if len(cleaned_all) == 13 and cleaned_all.isdigit():
        return cleaned_all
    if len(cleaned_all) == 10:
        converted = isbn10_to_isbn13(cleaned_all)
        return converted or cleaned_all

    return None


router = Router(name="alerts_router")


# ==============================================================================
# 1. PARCOURS UNIFIÉ /add (Semi-automatique BD & Comics)
# ==============================================================================


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

    # Pour un nombre entier pur, on n'interprète comme prix que s'il est >= 15 (exclut tome 1-10, Pi 4, PS 5...)
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
) -> str:
    """Formate le récapitulatif clair et sans bruit visuel avant confirmation."""
    price_str = (
        f"{max_price_cents / 100:.2f} €".replace(".", ",") if max_price_cents else "Aucun"
    )
    cat_emoji = CATEGORY_EMOJIS.get(CategorySlug(category_slug), "🎯")
    cat_label = CATEGORY_LABELS.get(CategorySlug(category_slug), "Alerte")

    lines = [
        f"{cat_emoji} <b>Récapitulatif de l'alerte ({cat_label})</b>\n",
        f"📌 <b>Nom :</b> {html.escape(title)}",
    ]
    if author:
        lines.append(f"✍️ <b>Auteur :</b> {html.escape(author)}")
    if publisher:
        lines.append(f"🏢 <b>Éditeur :</b> {html.escape(publisher)}")
    if isbn:
        lines.append(f"🔢 <b>ISBN :</b> <code>{isbn}</code>")

    lines.append(f"🎯 <b>Prix max :</b> {price_str}")
    lines.append(f"🔑 <b>Inclus :</b> {', '.join(include_keywords)}")
    if exclude_keywords:
        lines.append(f"🚫 <b>Exclus :</b> {', '.join(exclude_keywords)}")

    cond_display = ", ".join(conditions) if conditions else "Neuf avec étiquette, Neuf sans étiquette, Très bon état"
    lines.extend(
        [
            f"🏷️ <b>États :</b> {cond_display}\n",
            "Confirmer la création de cette alerte ?",
        ]
    )
    return "\n".join(lines)


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

    # Vérifier si un prix inline était fourni en fin de saisie (ex: "deathbringer 50" ou "978... 25")
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
        alert_repo = AlertRepository(db_session)
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
                "💶 <b>Pour modifier son prix max, entre un nouveau montant (ou clique sur Annuler) :</b>"
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
    category_repo = CategoryRepository(db_session)
    category = await category_repo.get_by_slug(CategorySlug.BD_COMICS.value)
    cat_id_str = str(category.id) if category else ""

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
    )

    if inline_price_cents is not None:
        await state.update_data(max_item_price_cents=inline_price_cents)
        await state.set_state(AlertCreationState.confirm_creation)
        recap_text = format_semi_auto_recap(
            title=meta.title,
            author=meta.author,
            publisher=meta.publisher,
            isbn=meta.isbn13,
            max_price_cents=inline_price_cents,
            include_keywords=meta.recommended_includes,
            exclude_keywords=meta.recommended_excludes,
        )
        await status_msg.edit_text(recap_text, reply_markup=get_alert_confirmation_keyboard())
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
        "🏷️ <b>États :</b> Neuf avec étiquette, Neuf sans étiquette, Très bon état\n\n"
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

    # Vérifier si un prix inline était fourni en fin de saisie (ex: "raspberry pi 4 50€" ou "deathbringer 50")
    clean_query, inline_price_cents = extract_inline_price_and_clean_query(query_str)

    cat_slug_enum = CategorySlug(category_slug)
    cat_emoji = CATEGORY_EMOJIS.get(cat_slug_enum, "🎯")
    cat_label = CATEGORY_LABELS.get(cat_slug_enum, "Produit")

    # Garde anti-doublon : Vérifier si ce produit est déjà surveillé
    if db_user:
        alert_repo = AlertRepository(db_session)
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
                if isinstance(event, CallbackQuery) and event.message and hasattr(event.message, "edit_text"):
                    await event.message.edit_text(warn_text, reply_markup=keyboard)
                else:
                    await target_msg.answer(warn_text, reply_markup=keyboard)
                return

            # Cas 2 : Pas de prix inline spécifié
            await state.update_data(existing_alert_id=str(existing.id))
            await state.set_state(AlertCreationState.waiting_for_max_price)
            warn_text = (
                f"⚠️ <b>Ce produit ({cat_label}) est déjà surveillé dans tes alertes !</b>\n\n"
                f"{cat_emoji} <b>{html.escape(existing.name)}</b>\n"
                f"🎯 <b>Prix max actuel :</b> {price_str}\n"
                f"{status_emoji} <b>Statut :</b> {status_text}\n\n"
                "💶 <b>Pour modifier son prix max, entre un nouveau montant (ou clique sur Annuler) :</b>"
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
            if isinstance(event, CallbackQuery) and event.message and hasattr(event.message, "edit_text"):
                await event.message.edit_text(warn_text, reply_markup=keyboard)
            else:
                await target_msg.answer(warn_text, reply_markup=keyboard)
            return

    # Catégorie en base
    category_repo = CategoryRepository(db_session)
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
        recap_text = format_semi_auto_recap(
            title=clean_query,
            author=None,
            publisher=None,
            isbn=None,
            max_price_cents=inline_price_cents,
            include_keywords=includes,
            exclude_keywords=default_excludes,
            category_slug=category_slug,
            conditions=default_conditions,
        )
        if isinstance(event, CallbackQuery) and event.message and hasattr(event.message, "edit_text"):
            await event.message.edit_text(recap_text, reply_markup=get_alert_confirmation_keyboard())
        else:
            await target_msg.answer(recap_text, reply_markup=get_alert_confirmation_keyboard())
        return

    # Demander le prix max
    await state.set_state(AlertCreationState.waiting_for_max_price)
    prompt = (
        f"{cat_emoji} <b>{html.escape(clean_query)}</b> ({cat_label})\n\n"
        f"🔑 <b>Inclus :</b> {', '.join(includes)}\n"
        f"🚫 <b>Exclus :</b> {', '.join(default_excludes)}\n"
        f"🏷️ <b>États :</b> {', '.join(default_conditions)}\n\n"
        "💶 <b>Indique ton prix maximum pour cette alerte (en €)</b> <i>(ou /cancel)</i> :"
    )
    if isinstance(event, CallbackQuery) and event.message and hasattr(event.message, "edit_text"):
        await event.message.edit_text(prompt)
    else:
        await target_msg.answer(prompt)


@router.message(Command("add"))
@router.message(Command("addisbn"))
@router.message(Command("addurl"))
@router.callback_query(F.data == "btn_add_alert")
async def start_add_alert(
    event: Message | CallbackQuery,
    state: FSMContext,
    db_session: Any,
    db_user: User,
) -> None:
    """Point d'entrée unifié pour l'ajout d'alertes multi-catégories."""
    await state.clear()

    inline_query: str | None = None
    if isinstance(event, Message):
        raw_text = (event.text or "").strip()
        parts = raw_text.split(maxsplit=1)
        if len(parts) > 1:
            inline_query = parts[1].strip()

    if inline_query:
        await state.update_data(inline_query=inline_query)
        prompt_text = (
            f"🎯 <b>Ajout d'une alerte pour « {html.escape(inline_query)} »</b>\n\n"
            "Choisis la catégorie de ce produit :"
        )
    else:
        prompt_text = (
            "🎯 <b>Création d'une nouvelle alerte</b>\n\n"
            "Choisis la catégorie du produit que tu souhaites surveiller :"
        )

    await state.set_state(AlertCreationState.waiting_for_category)
    keyboard = get_category_selection_keyboard()

    if isinstance(event, CallbackQuery):
        await event.answer()
        if event.message:
            await event.message.answer(prompt_text, reply_markup=keyboard)
    else:
        await event.answer(prompt_text, reply_markup=keyboard)


@router.callback_query(AlertCreationState.waiting_for_category, F.data.startswith("add_cat:"))
async def handle_category_selected(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: Any,
    db_user: User,
) -> None:
    """Traite la sélection de catégorie et oriente vers le flux adapté."""
    await callback.answer()
    category_slug = callback.data.split(":")[1]  # type: ignore

    data = await state.get_data()
    inline_query = data.get("inline_query")

    if inline_query:
        if category_slug == CategorySlug.BD_COMICS.value:
            await process_bd_query_lookup(callback, state, inline_query, db_session, db_user=db_user)
        else:
            await process_tech_or_clothes_query(
                callback, state, inline_query, category_slug, db_session, db_user=db_user
            )
        return

    # Pas d'inline_query, demander la saisie du produit / titre selon la catégorie
    await state.update_data(category_slug=category_slug)
    await state.set_state(AlertCreationState.waiting_for_query)

    if category_slug == CategorySlug.BD_COMICS.value:
        prompt = (
            "📚 <b>Alerte Livres & BD</b>\n\n"
            "Entre le titre de l'œuvre que tu souhaites surveiller (avec auteur optionnel) :\n\n"
            "<i>(Tape /cancel à tout moment pour annuler)</i>"
        )
    elif category_slug == CategorySlug.TECH.value:
        prompt = (
            "💻 <b>Alerte Électronique</b>\n\n"
            "Quel produit ou modèle électronique recherches-tu ?\n"
            "<i>(ex: Raspberry Pi 4, Steam Deck 512Go, Sony WH-1000XM4...)</i>\n\n"
            "<i>(Tape /cancel à tout moment pour annuler)</i>"
        )
    elif category_slug == CategorySlug.GAMING.value:
        prompt = (
            "🎮 <b>Alerte Jeux Vidéo</b>\n\n"
            "Quel jeu ou console recherches-tu ?\n"
            "<i>(ex: Mario Kart 8 Deluxe Switch, Zelda Tears of the Kingdom, Nintendo Switch Oled...)</i>\n\n"
            "<i>(Tape /cancel à tout moment pour annuler)</i>"
        )
    else:
        prompt = (
            "👕 <b>Alerte Vêtements</b>\n\n"
            "Quel vêtement ou modèle recherches-tu ?\n"
            "<i>(ex: Veste Carhartt Detroit, Pull Ralph Lauren M...)</i>\n\n"
            "<i>(Tape /cancel à tout moment pour annuler)</i>"
        )

    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(prompt)


@router.message(AlertCreationState.waiting_for_query)
async def handle_query_input(
    message: Message,
    state: FSMContext,
    db_session: Any,
    db_user: User,
) -> None:
    """Traite la saisie du titre/produit et oriente vers la recherche adaptée."""
    query = (message.text or "").strip()
    if not query or len(query) < 2:
        await message.answer("La recherche doit contenir au moins 2 caractères. Réessaie :")
        return

    data = await state.get_data()
    category_slug = data.get("category_slug", CategorySlug.BD_COMICS.value)

    if category_slug == CategorySlug.BD_COMICS.value:
        await process_bd_query_lookup(message, state, query, db_session, db_user=db_user)
    else:
        await process_tech_or_clothes_query(
            message, state, query, category_slug, db_session, db_user=db_user
        )


@router.callback_query(F.data.startswith("upd_alert_price:"))
async def handle_update_existing_alert_price(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: Any,
) -> None:
    """Met à jour directement le prix d'une alerte déjà existante."""
    await callback.answer()
    parts = callback.data.split(":")  # type: ignore
    alert_id_str = parts[1]
    price_cents = int(parts[2])

    alert_repo = AlertRepository(db_session)
    existing = await alert_repo.get_by_id(uuid.UUID(alert_id_str))
    if existing:
        existing.max_item_price_cents = price_cents
        await db_session.commit()
        await state.clear()
        new_price_str = f"{price_cents / 100:.2f} €".replace(".", ",")
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"✅ <b>Prix max mis à jour !</b>\n\n"
                f"📚 <b>{html.escape(existing.name)}</b>\n"
                f"🎯 <b>Nouveau prix max :</b> {new_price_str}\n\n"
                f"⚡ <i>L'alerte existante a été actualisée sans créer de doublon.</i>"
            )
    else:
        await state.clear()


@router.message(AlertCreationState.waiting_for_max_price)
async def process_max_price_input(
    message: Message,
    state: FSMContext,
    db_session: Any,
) -> None:
    """Valide le montant maximum et affiche le récapitulatif pour confirmation."""
    text = (message.text or "").strip()
    try:
        price_cents = parse_price_to_cents(text)
    except AlertServiceError as exc:
        await message.answer(f"⚠️ {exc}\nMerci d'indiquer un montant valide (ex: 25 ou 50) :")
        return

    data = await state.get_data()
    existing_id = data.get("existing_alert_id")
    if existing_id:
        alert_repo = AlertRepository(db_session)
        existing = await alert_repo.get_by_id(uuid.UUID(existing_id))
        if existing:
            existing.max_item_price_cents = price_cents
            await db_session.commit()
            await state.clear()
            new_price_str = f"{price_cents / 100:.2f} €".replace(".", ",")
            await message.answer(
                f"✅ <b>Prix max mis à jour !</b>\n\n"
                f"📚 <b>{html.escape(existing.name)}</b>\n"
                f"🎯 <b>Nouveau prix max :</b> {new_price_str}\n\n"
                f"⚡ <i>L'alerte existante a été actualisée sans créer de doublon.</i>"
            )
            return

    await state.update_data(max_item_price_cents=price_cents)
    await state.set_state(AlertCreationState.confirm_creation)

    recap_text = format_semi_auto_recap(
        title=data.get("name", "Alerte"),
        author=data.get("author"),
        publisher=data.get("publisher"),
        isbn=data.get("isbn"),
        max_price_cents=price_cents,
        include_keywords=data.get("include_keywords", []),
        exclude_keywords=data.get("exclude_keywords", []),
        category_slug=data.get("category_slug", CategorySlug.BD_COMICS.value),
        conditions=data.get("selected_conditions"),
    )
    await message.answer(recap_text, reply_markup=get_alert_confirmation_keyboard())


@router.callback_query(AlertCreationState.confirm_creation, F.data == "alert_create_confirm")
async def save_alert(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: User,
    db_session: Any,
) -> None:
    """Enregistre l'alerte validée en base de données, lie le livre et préchauffe la cote."""
    await callback.answer()
    data = await state.get_data()

    category_slug = data.get("category_slug", CategorySlug.BD_COMICS.value)
    name = data.get("name") or "Alerte"
    isbn = data.get("isbn")
    author = data.get("author")
    publisher = data.get("publisher")
    includes = data.get("include_keywords", [])
    excludes = data.get("exclude_keywords", [])
    variants = data.get("variants", [])
    max_price = data.get("max_item_price_cents")
    conditions = data.get("selected_conditions") or (
        list(DEFAULT_BD_CONDITIONS)
        if category_slug == CategorySlug.BD_COMICS.value
        else (
            list(DEFAULT_GAMING_CONDITIONS)
            if category_slug == CategorySlug.GAMING.value
            else (
                list(DEFAULT_TECH_CONDITIONS)
                if category_slug == CategorySlug.TECH.value
                else list(DEFAULT_CLOTHES_CONDITIONS)
            )
        )
    )
    cat_id_str = data.get("category_id")

    # 1. Catégorie
    category_repo = CategoryRepository(db_session)
    category = None
    if cat_id_str:
        try:
            category = await category_repo.get_by_id(uuid.UUID(cat_id_str))
        except Exception:
            pass
    if not category:
        category = await category_repo.get_by_slug(category_slug)

    alert_repo = AlertRepository(db_session)

    if category_slug == CategorySlug.BD_COMICS.value:
        # 2. Livre dans le catalogue
        book_repo = BookRepository(db_session)
        book = None
        if isbn:
            book = await book_repo.get_by_isbn13(isbn)
        if not book:
            book = await book_repo.create(
                title=name,
                normalized_title=normalize_text(name),
                isbn13=isbn,
                author=author,
                publisher=publisher,
            )

        # 3. Création de l'alerte
        vinted_url = (
            f"https://www.vinted.fr/catalog?search_start_type=item_page_isbn&search_text={isbn}&order=newest_first"
            if isbn
            else f"https://www.vinted.fr/catalog?search_text={urllib.parse.quote_plus(name)}&order=newest_first"
        )

        alert = await alert_repo.create(
            user_id=db_user.id,
            category_id=category.id,
            name=name,
            isbn=isbn,
            include_keywords=includes,
            exclude_keywords=excludes,
            variants=variants,
            max_item_price_cents=max_price,
            accepted_conditions=conditions,
            vinted_search_url=vinted_url,
        )

        # 4. Liaison alerte <-> livre
        if book:
            await book_repo.link_alert_to_book(alert.id, book.id, MatchConfidence.HIGH)

        # 5. Préchauffage et enregistrement de la cote du marché
        market_repo = MarketPriceRepository(db_session)
        best_offer = None
        hist_low = None
        try:
            provider = ChasseAuxLivresProvider()
            offers = await provider.get_offers(isbn13=isbn, title=name)
            if offers and book:
                for off in offers:
                    await market_repo.save_snapshot(book.id, off)
                best_offer = ProviderRegistry.select_best_offer(offers)
                if best_offer:
                    hist_low = (
                        best_offer.raw_payload.get("historical_low_1y_cents")
                        if best_offer.raw_payload
                        else None
                    )
                    await market_repo.update_or_create_cache(
                        book_id=book.id,
                        lowest_delivered_price_cents=best_offer.delivered_price_cents,
                        best_provider_name=best_offer.provider_name,
                        best_source_url=best_offer.source_url,
                        confidence=best_offer.confidence,
                        comparison_status=ComparisonStatus.AVAILABLE,
                        cache_duration_hours=24,
                        historical_low_1y_cents=hist_low,
                    )
        except Exception as exc:
            logger.warning("Erreur Chasse-aux-livres lors de la création d'alerte : %s", exc)

        await db_session.commit()
        await state.clear()

        price_str = f"{max_price / 100:.2f} €".replace(".", ",") if max_price else "Aucun"
        lowest_str = (
            f"{best_offer.delivered_price_cents / 100:.2f} € ({best_offer.provider_name})".replace(".", ",")
            if best_offer and best_offer.delivered_price_cents
            else "Recherche en cours"
        )
        hist_low_str = f"{hist_low / 100:.2f} €".replace(".", ",") if hist_low else "Non renseigné"

        resp_text = (
            "✅ <b>Alerte créée avec succès !</b>\n\n"
            f"📚 <b>{html.escape(name)}</b>\n"
            f"🔢 <b>ISBN :</b> <code>{isbn or 'N/A'}</code>\n"
            f"🎯 <b>Prix max :</b> {price_str}\n"
            f"📉 <b>Prix du marché :</b> {lowest_str}\n"
            f"🏷️ <b>Plus bas constaté (1 an) :</b> {hist_low_str}\n\n"
            "⚡ <i>Surveillance active 24h/24 sur Vinted. Vous recevrez une notification instantanée dès qu'une offre éligible apparaît.</i>"
        )
    else:
        # Tech ou Vêtements (sans livre ni cote de livres)
        vinted_url = f"https://www.vinted.fr/catalog?search_text={urllib.parse.quote_plus(name)}&order=newest_first"
        alert = await alert_repo.create(
            user_id=db_user.id,
            category_id=category.id,
            name=name,
            isbn=None,
            include_keywords=includes,
            exclude_keywords=excludes,
            variants=variants,
            max_item_price_cents=max_price,
            accepted_conditions=conditions,
            vinted_search_url=vinted_url,
        )
        await db_session.commit()
        await state.clear()

        cat_slug_enum = CategorySlug(category_slug)
        cat_emoji = CATEGORY_EMOJIS.get(cat_slug_enum, "🎯")
        cat_label = CATEGORY_LABELS.get(cat_slug_enum, "Produit")
        price_str = f"{max_price / 100:.2f} €".replace(".", ",") if max_price else "Aucun"
        cond_str = ", ".join(conditions) if conditions else "Tous"

        resp_text = (
            "✅ <b>Alerte créée avec succès !</b>\n\n"
            f"{cat_emoji} <b>{html.escape(name)}</b> ({cat_label})\n"
            f"🎯 <b>Prix max :</b> {price_str}\n"
            f"🔑 <b>Mots-clés requis :</b> {', '.join(includes)}\n"
            f"🚫 <b>Exclusions :</b> {', '.join(excludes) if excludes else 'Aucune'}\n"
            f"🏷️ <b>États acceptés :</b> {cond_str}\n\n"
            "⚡ <i>Surveillance active 24h/24 sur Vinted. Vous recevrez une notification instantanée dès qu'une offre éligible apparaît.</i>"
        )

    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(resp_text)


@router.message(Command("cancel", "annuler"), StateFilter("*"))
@router.message(
    F.text.casefold().in_({"/cancel", "/annuler", "cancel", "annuler"}),
    StateFilter("*"),
)
async def handle_cancel_command(message: Message, state: FSMContext) -> None:
    """Annule la création ou la modification d'alerte en cours à tout moment."""
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("ℹ️ Aucune opération en cours à annuler.")
        return

    await state.clear()
    await message.answer("❌ Opération annulée avec succès.")


@router.callback_query(F.data == "alert_create_cancel")
async def cancel_alert_creation(callback: CallbackQuery, state: FSMContext) -> None:
    """Annule la création de l'alerte via bouton inline."""
    await callback.answer()
    await state.clear()
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text("❌ Création de l'alerte annulée.")


@router.callback_query(F.data.in_(["alert_create_edit", "alert_create_edit_price"]))
async def edit_alert_creation_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Permet de modifier le prix max ou de recommencer la configuration."""
    await callback.answer()
    data = await state.get_data()
    name = data.get("name", "cet article")
    current_price = data.get("max_item_price_cents")
    current_price_str = (
        f"{current_price / 100:.2f} €".replace(".", ",") if current_price else "Aucun"
    )

    text = (
        f"✏️ <b>Modification de l'alerte « {html.escape(name)} »</b>\n\n"
        f"🎯 <b>Prix max actuel :</b> {current_price_str}\n\n"
        "Que souhaites-tu faire ?"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💶 Modifier le prix max", callback_data="al_edit_step:price"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Changer de produit / Recommencer",
                    callback_data="al_edit_step:restart",
                )
            ],
            [
                InlineKeyboardButton(
                    text="← Retour au récapitulatif", callback_data="al_edit_step:back"
                )
            ],
        ]
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data == "al_edit_step:price")
async def prompt_new_price(callback: CallbackQuery, state: FSMContext) -> None:
    """Demande la saisie d'un nouveau montant."""
    await callback.answer()
    await state.set_state(AlertCreationState.waiting_for_max_price)
    text = (
        "💶 <b>Entre le nouveau montant maximum pour cette alerte (en €) :</b>\n\n"
        "<i>(ex: 15 ou 25)</i>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← Retour au récapitulatif", callback_data="al_edit_step:back"
                )
            ]
        ]
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data == "al_edit_step:back")
async def back_to_recap(callback: CallbackQuery, state: FSMContext) -> None:
    """Retourne directement au récapitulatif avec les boutons de confirmation."""
    await callback.answer()
    data = await state.get_data()
    await state.set_state(AlertCreationState.confirm_creation)

    recap_text = format_semi_auto_recap(
        title=data.get("name", "Alerte"),
        author=data.get("author"),
        publisher=data.get("publisher"),
        isbn=data.get("isbn"),
        max_price_cents=data.get("max_item_price_cents"),
        include_keywords=data.get("include_keywords", []),
        exclude_keywords=data.get("exclude_keywords", []),
        category_slug=data.get("category_slug", CategorySlug.BD_COMICS.value),
        conditions=data.get("selected_conditions"),
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(
            recap_text, reply_markup=get_alert_confirmation_keyboard()
        )


@router.callback_query(F.data == "al_edit_step:restart")
async def restart_from_scratch(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Réinitialise et réaffiche le choix des catégories en éditant le message existant."""
    await callback.answer()
    await state.clear()
    await state.set_state(AlertCreationState.waiting_for_category)
    prompt_text = (
        "🎯 <b>Création d'une nouvelle alerte</b>\n\n"
        "Choisis la catégorie du produit que tu souhaites surveiller :"
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(
            prompt_text, reply_markup=get_category_selection_keyboard()
        )


# ==============================================================================
# 2. AFFICHAGE PAGINÉ /show
# ==============================================================================


@router.message(Command("show"))
async def show_alerts(
    message: Message,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche la première alerte active de l'utilisateur avec pagination ou recherche par terme/ISBN."""
    alert_repo = AlertRepository(db_session)

    # Vérifier si l'utilisateur a passé un terme de recherche (ex: /show Soli ou /show 9791034768974)
    args = (message.text or "").strip().split(maxsplit=1)
    if len(args) > 1:
        query = args[1].strip()
        matched_alerts = await alert_repo.search_by_user(db_user.id, query=query, limit=10)
        if not matched_alerts:
            await message.answer(
                f"❌ Aucune alerte trouvée correspondant à « <b>{query}</b> ».\n\n"
                "Tape /show pour faire défiler toutes tes alertes ou /add pour en créer une."
            )
            return

        if len(matched_alerts) == 1:
            alert = matched_alerts[0]
            card_text = format_alert_card(alert, current_index=0, total_count=1)
            keyboard = get_alert_card_keyboard(
                alert.id, alert.status, current_page=0, total_pages=1
            )
            await message.answer(card_text, reply_markup=keyboard)
            return

        # Plusieurs correspondances : afficher la liste avec boutons d'accès direct
        buttons = [
            [
                InlineKeyboardButton(
                    text=f"{a.name} ({'Actif' if a.status == AlertStatus.ACTIVE else 'En pause'})",
                    callback_data=f"al_view:{a.id}",
                )
            ]
            for a in matched_alerts
        ]
        buttons.append(
            [InlineKeyboardButton(text="📋 Toutes mes alertes", callback_data="al_page:0")]
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(
            f"🔍 <b>{len(matched_alerts)} alertes correspondent à « {query} » :</b>\n\n"
            "Clique sur celle que tu souhaites consulter :",
            reply_markup=keyboard,
        )
        return

    # Comportement par défaut sans argument : pagination complète
    total_count = await alert_repo.count_by_user(db_user.id)
    if total_count == 0:
        await message.answer("Aucune alerte active pour le moment. Utilise /add pour en créer une.")
        return

    alerts = await alert_repo.list_by_user(db_user.id, limit=1, offset=0)
    alert = alerts[0]

    card_text = format_alert_card(alert, current_index=0, total_count=total_count)
    keyboard = get_alert_card_keyboard(
        alert.id, alert.status, current_page=0, total_pages=total_count
    )
    await message.answer(card_text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("al_page:"))
async def paginate_alerts(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Navigue entre les cartes d'alertes."""
    await callback.answer()
    parts = callback.data.split(":")  # type: ignore
    if len(parts) == 2:
        category_slug = "all"
        page = int(parts[1])
    else:
        category_slug = parts[1]
        page = int(parts[2])

    alert_repo = AlertRepository(db_session)
    total_count = await alert_repo.count_by_user(db_user.id, category_slug=category_slug)
    if total_count == 0:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "Aucune alerte trouvée dans cette catégorie.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="📁 Retour aux catégories", callback_data="ed_cat_menu"
                            )
                        ]
                    ]
                ),
            )
        return

    page = max(0, min(page, total_count - 1))
    alerts = await alert_repo.list_by_user(
        db_user.id, limit=1, offset=page, category_slug=category_slug
    )
    if not alerts:
        return

    alert = alerts[0]
    card_text = format_alert_card(alert, current_index=page, total_count=total_count)
    keyboard = get_alert_card_keyboard(
        alert.id,
        alert.status,
        current_page=page,
        total_pages=total_count,
        category_slug=category_slug,
    )

    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(card_text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("al_view:"))
async def view_single_alert(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche la carte d'une alerte spécifique après action ou retour."""
    await callback.answer()
    alert_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore

    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert or alert.status == AlertStatus.DELETED:
        if callback.message and hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(
                "Alerte introuvable.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📁 Catégories", callback_data="ed_cat_menu")]
                    ]
                ),
            )
        return

    cat_slug = alert.category.slug if alert.category else "all"
    total_count = await alert_repo.count_by_user(db_user.id, category_slug=cat_slug)
    card_text = format_alert_card(alert, current_index=0, total_count=total_count)
    keyboard = get_alert_card_keyboard(
        alert.id,
        alert.status,
        current_page=0,
        total_pages=total_count,
        category_slug=cat_slug,
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(card_text, reply_markup=keyboard)


CATEGORY_ARG_MAP = {
    "bd": CategorySlug.BD_COMICS.value,
    "bd_comics": CategorySlug.BD_COMICS.value,
    "livre": CategorySlug.BD_COMICS.value,
    "livres": CategorySlug.BD_COMICS.value,
    "book": CategorySlug.BD_COMICS.value,
    "books": CategorySlug.BD_COMICS.value,
    "tech": CategorySlug.TECH.value,
    "electronique": CategorySlug.TECH.value,
    "électronique": CategorySlug.TECH.value,
    "gaming": CategorySlug.GAMING.value,
    "jeux": CategorySlug.GAMING.value,
    "jeu": CategorySlug.GAMING.value,
    "jeuxvideo": CategorySlug.GAMING.value,
    "jeux_video": CategorySlug.GAMING.value,
    "game": CategorySlug.GAMING.value,
    "games": CategorySlug.GAMING.value,
    "clothes": CategorySlug.CLOTHES.value,
    "vetement": CategorySlug.CLOTHES.value,
    "vetements": CategorySlug.CLOTHES.value,
    "vêtement": CategorySlug.CLOTHES.value,
    "vêtements": CategorySlug.CLOTHES.value,
    "all": "all",
    "tout": "all",
    "toutes": "all",
}


def format_alerts_list_text(
    alerts: list,
    current_page: int,
    total_count: int,
    category_label: str | None = None,
) -> str:
    """Génère le texte de la vue compacte /list."""
    total_pages = max(1, (total_count + 9) // 10)
    title_suffix = f" — {category_label}" if category_label else ""
    lines = [
        f"📋 <b>Vos alertes surveillées{title_suffix}</b> (Page {current_page + 1}/{total_pages} — {total_count} alerte{'s' if total_count > 1 else ''}) :\n"
    ]
    start_num = current_page * 10 + 1
    for i, a in enumerate(alerts, start=start_num):
        status_icon = "🟢" if a.status == AlertStatus.ACTIVE else "⏸️"
        cat_icon = f"{a.category.emoji} " if getattr(a, "category", None) and getattr(a.category, "emoji", None) else ""
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


async def show_list_categories(
    target: Message | CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche le menu de sélection de catégorie pour /list."""
    alert_repo = AlertRepository(db_session)
    total_count = await alert_repo.count_by_user(db_user.id)
    if total_count == 0:
        msg = "Aucune alerte active pour le moment. Utilise /add pour en créer une."
        if isinstance(target, CallbackQuery):
            await target.answer()
            if target.message and hasattr(target.message, "edit_text"):
                await target.message.edit_text(msg)
        else:
            await target.answer(msg)
        return

    cat_repo = CategoryRepository(db_session)
    active_categories = await cat_repo.get_all_active()
    counts = await alert_repo.count_by_user_grouped_by_category(db_user.id)

    text = (
        "📋 <b>Vos alertes surveillées</b>\n\n"
        "Choisissez une catégorie pour afficher vos alertes :"
    )
    keyboard = get_list_category_selection_keyboard(active_categories, counts, total_count)

    if isinstance(target, CallbackQuery):
        await target.answer()
        if target.message and hasattr(target.message, "edit_text"):
            await target.message.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


async def display_alerts_list(
    target: Message | CallbackQuery,
    db_user: User,
    db_session: Any,
    category_slug: str = "all",
    page: int = 0,
) -> None:
    """Affiche la liste paginée pour une catégorie donnée."""
    alert_repo = AlertRepository(db_session)
    total_count = await alert_repo.count_by_user(db_user.id, category_slug=category_slug)
    if total_count == 0:
        msg = "Aucune alerte trouvée dans cette catégorie."
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📁 Choisir une autre catégorie", callback_data="al_list_cat_menu")],
                [InlineKeyboardButton(text="❌ Fermer", callback_data="al_close")],
            ]
        )
        if isinstance(target, CallbackQuery):
            await target.answer()
            if target.message and hasattr(target.message, "edit_text"):
                await target.message.edit_text(msg, reply_markup=reply_markup)
        else:
            await target.answer(msg, reply_markup=reply_markup)
        return

    cat_label = None
    if category_slug and category_slug != "all":
        cat_repo = CategoryRepository(db_session)
        cat_obj = await cat_repo.get_by_slug(category_slug)
        if cat_obj:
            cat_label = f"{cat_obj.emoji} {cat_obj.label}"

    total_pages = max(1, (total_count + 9) // 10)
    page = max(0, min(page, total_pages - 1))
    alerts = await alert_repo.list_by_user(
        db_user.id, limit=10, offset=page * 10, category_slug=category_slug
    )

    text = format_alerts_list_text(
        alerts, current_page=page, total_count=total_count, category_label=cat_label
    )
    keyboard = get_alerts_list_keyboard(
        alerts, current_page=page, total_pages=total_pages, category_slug=category_slug
    )

    if isinstance(target, CallbackQuery):
        await target.answer()
        if target.message and hasattr(target.message, "edit_text"):
            await target.message.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


@router.message(Command("list"))
async def list_alerts(
    message: Message,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche une vue d'ensemble compacte et paginée des alertes, par catégorie ou globale."""
    args = (message.text or "").strip().split(maxsplit=1)
    if len(args) > 1:
        raw_arg = args[1].lower().strip()
        matched_slug = CATEGORY_ARG_MAP.get(raw_arg)
        if matched_slug:
            await display_alerts_list(message, db_user, db_session, category_slug=matched_slug, page=0)
            return

    await show_list_categories(message, db_user, db_session)


@router.callback_query(F.data == "al_list_cat_menu")
async def handle_list_category_menu(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Retourne au menu de sélection de catégorie pour /list."""
    await show_list_categories(callback, db_user, db_session)


@router.callback_query(F.data.startswith("al_list_cat:"))
async def handle_select_list_category(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche la liste paginée pour la catégorie sélectionnée."""
    parts = callback.data.split(":")  # type: ignore
    category_slug = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0
    await display_alerts_list(callback, db_user, db_session, category_slug=category_slug, page=page)


@router.callback_query(F.data.startswith("al_list_page:"))
async def paginate_alerts_list(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Navigue entre les pages de la liste des alertes."""
    parts = callback.data.split(":")  # type: ignore
    if len(parts) >= 3:
        category_slug = parts[1]
        page = int(parts[2])
    else:
        category_slug = "all"
        page = int(parts[1])

    await display_alerts_list(callback, db_user, db_session, category_slug=category_slug, page=page)


# ==============================================================================
# 3. MODIFICATION /edit
# ==============================================================================


@router.message(Command("edit"))
async def handle_edit_command(
    message: Message,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche le sélecteur de catégorie pour parcourir et modifier les alertes."""
    args = (message.text or "").strip().split(maxsplit=1)
    if len(args) > 1:
        await show_alerts(message, db_user, db_session)
        return

    await show_edit_categories(message, db_user, db_session)


async def show_edit_categories(
    target: Message | CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche le menu de sélection de catégorie pour /edit."""
    alert_repo = AlertRepository(db_session)
    total_count = await alert_repo.count_by_user(db_user.id)
    if total_count == 0:
        msg = "Aucune alerte active pour le moment. Utilise /add pour en créer une."
        if isinstance(target, CallbackQuery):
            if target.message and hasattr(target.message, "edit_text"):
                await target.message.edit_text(msg)
        else:
            await target.answer(msg)
        return

    cat_repo = CategoryRepository(db_session)
    active_categories = await cat_repo.get_all_active()
    counts = await alert_repo.count_by_user_grouped_by_category(db_user.id)

    text = (
        "📁 <b>Gestion et modification de vos alertes</b>\n\n"
        "Choisissez une catégorie pour afficher et modifier vos alertes :"
    )
    keyboard = get_edit_category_selection_keyboard(active_categories, counts, total_count)

    if isinstance(target, CallbackQuery):
        await target.answer()
        if target.message and hasattr(target.message, "edit_text"):
            await target.message.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "ed_cat_menu")
async def handle_edit_category_menu(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Retourne au sélecteur de catégorie dans /edit."""
    await show_edit_categories(callback, db_user, db_session)


@router.callback_query(F.data.startswith("ed_cat:"))
async def handle_select_edit_category(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche la première alerte de la catégorie choisie."""
    await callback.answer()
    parts = callback.data.split(":")  # type: ignore
    category_slug = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0

    alert_repo = AlertRepository(db_session)
    total_count = await alert_repo.count_by_user(db_user.id, category_slug=category_slug)
    if total_count == 0:
        await callback.answer("Aucune alerte dans cette catégorie.", show_alert=True)
        return

    page = max(0, min(page, total_count - 1))
    alerts = await alert_repo.list_by_user(
        db_user.id, limit=1, offset=page, category_slug=category_slug
    )
    if not alerts:
        await callback.answer("Aucune alerte trouvée.", show_alert=True)
        return

    alert = alerts[0]
    card_text = format_alert_card(alert, current_index=page, total_count=total_count)
    keyboard = get_alert_card_keyboard(
        alert.id,
        alert.status,
        current_page=page,
        total_pages=total_count,
        category_slug=category_slug,
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(card_text, reply_markup=keyboard)


@router.callback_query(F.data == "al_close")
async def handle_close_alert_menu(callback: CallbackQuery) -> None:
    """Ferme le menu d'alertes."""
    await callback.answer()
    if callback.message and hasattr(callback.message, "delete"):
        await callback.message.delete()


@router.callback_query(F.data.startswith("al_edit:"))
async def select_edit_field(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche les options de modification pour une alerte donnée."""
    await callback.answer()
    alert_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore

    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    cat_slug = alert.category.slug if alert.category else None
    cat_emoji = alert.category.emoji if alert.category else "🎯"
    text = (
        f"Modifier l'alerte {cat_emoji} « <b>{html.escape(alert.name)}</b> ».\n"
        "Choisis le critère à mettre à jour :"
    )
    keyboard = get_alert_edit_fields_keyboard(alert.id, category_slug=cat_slug)
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("ed_f:"))
async def prompt_new_field_value(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Demande la nouvelle valeur pour le champ sélectionné."""
    await callback.answer()
    parts = callback.data.split(":")  # type: ignore
    alert_id = parts[1]
    field = parts[2]

    await state.set_state(AlertEditState.waiting_for_new_value)
    await state.update_data(edit_alert_id=alert_id, edit_field=field)

    prompts = {
        "name": "Envoie le nouveau nom / titre :",
        "isbn": "Envoie le nouvel ISBN-10 ou ISBN-13 (ex: 9782381961903) :",
        "url": "Envoie le nouveau lien Vinted (HTTPS) :",
        "include_keywords": "Envoie les nouveaux mots-clés inclus (séparés par des virgules) :",
        "exclude_keywords": "Envoie les nouveaux mots-clés à exclure (ou /skip pour aucun) :",
        "variants": "Envoie les nouvelles variantes (ou /skip pour aucune) :",
        "max_price": "Envoie le nouveau prix maximum de l'article en euros (ex: 15) :",
        "conditions": (
            "Envoie les états acceptés séparés par des virgules\n"
            "(ex: Neuf avec étiquette, Très bon état) :"
        ),
    }
    msg = prompts.get(field, "Envoie la nouvelle valeur :")
    if callback.message and hasattr(callback.message, "answer"):
        await callback.message.answer(msg)


@router.message(AlertEditState.waiting_for_new_value)
async def save_edited_field_value(
    message: Message,
    state: FSMContext,
    db_user: User,
    db_session: Any,
) -> None:
    """Applique la modification du champ en base de données."""
    data = await state.get_data()
    alert_id = uuid.UUID(data["edit_alert_id"])
    field = data["edit_field"]
    text = (message.text or "").strip()

    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await message.answer("Alerte introuvable.")
        await state.clear()
        return

    try:
        if field == "name":
            if len(text) < 2:
                raise AlertServiceError("Le nom doit contenir au moins 2 caractères.")
            alert.name = text
            if alert.category and alert.category.slug in (
                CategorySlug.TECH.value,
                CategorySlug.CLOTHES.value,
                CategorySlug.GAMING.value,
            ):
                alert.vinted_search_url = (
                    f"https://www.vinted.fr/catalog?search_text={urllib.parse.quote_plus(text)}&order=newest_first"
                )
        elif field == "isbn":
            clean_isbn = extract_clean_isbn(text)
            if not clean_isbn:
                raise AlertServiceError("Format d'ISBN invalide (doit comporter 10 ou 13 chiffres).")
            alert.isbn = clean_isbn
            if clean_isbn not in alert.include_keywords:
                alert.include_keywords = [clean_isbn] + [
                    kw for kw in alert.include_keywords if not kw.isdigit()
                ]
        elif field == "url":
            parsed = validate_and_parse_vinted_url(text)
            alert.vinted_search_url = parsed["url"]
        elif field == "include_keywords":
            kw = parse_comma_separated_list(text)
            if not kw:
                raise AlertServiceError("Au moins un mot-clé est requis.")
            alert.include_keywords = kw
        elif field == "exclude_keywords":
            alert.exclude_keywords = (
                [] if text.lower() == "/skip" else parse_comma_separated_list(text)
            )
        elif field == "variants":
            alert.variants = [] if text.lower() == "/skip" else parse_comma_separated_list(text)
        elif field == "max_price":
            alert.max_item_price_cents = parse_price_to_cents(text)
        elif field == "conditions":
            alert.accepted_conditions = parse_comma_separated_list(text) if text else None

        await db_session.commit()
        await state.clear()

        cat_slug = alert.category.slug if alert.category else "all"
        total_count = await alert_repo.count_by_user(db_user.id, category_slug=cat_slug)

        await message.answer(f"✅ Champ <b>{field}</b> mis à jour avec succès !")
        card_text = format_alert_card(alert, current_index=0, total_count=total_count)
        await message.answer(
            card_text,
            reply_markup=get_alert_card_keyboard(
                alert.id,
                alert.status,
                current_page=0,
                total_pages=total_count,
                category_slug=cat_slug,
            ),
        )
    except AlertServiceError as exc:
        await message.answer(f"⚠️ {exc}\nMerci de réessayer :")


# ==============================================================================
# 4. GESTION STATUT /pause ET /resume
# ==============================================================================


@router.message(Command("pause"))
async def handle_pause_command(message: Message, db_user: User, db_session: Any) -> None:
    """Affiche les alertes actives pour mise en pause."""
    await show_alerts(message, db_user, db_session)


@router.callback_query(F.data.startswith("al_pause:"))
async def toggle_pause_alert(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Met en pause une alerte active."""
    alert_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore
    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    await alert_repo.set_status(alert, AlertStatus.PAUSED)
    await db_session.commit()
    await callback.answer("⏸ Alerte mise en pause.")
    await view_single_alert(callback, db_user, db_session)


@router.message(Command("resume"))
async def handle_resume_command(message: Message, db_user: User, db_session: Any) -> None:
    """Affiche les alertes pour réactivation."""
    await show_alerts(message, db_user, db_session)


@router.callback_query(F.data.startswith("al_resume:"))
async def toggle_resume_alert(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Réactive une alerte mise en pause."""
    alert_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore
    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    await alert_repo.set_status(alert, AlertStatus.ACTIVE)
    await db_session.commit()
    await callback.answer("▶️ Alerte réactivée.")
    await view_single_alert(callback, db_user, db_session)


# ==============================================================================
# 5. SUPPRESSION /delete AVEC CONFIRMATION (SOFT DELETE)
# ==============================================================================


@router.message(Command("delete"))
async def handle_delete_command(message: Message, db_user: User, db_session: Any) -> None:
    """Affiche les alertes pour suppression."""
    await show_alerts(message, db_user, db_session)


@router.callback_query(F.data.startswith("al_del:"))
async def prompt_delete_confirmation(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Demande la confirmation obligatoire avant la suppression."""
    await callback.answer()
    alert_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore

    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    msg = f"Supprimer définitivement l'alerte « <b>{alert.name}</b> » ?"
    keyboard = get_delete_confirmation_keyboard(alert.id)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(msg, reply_markup=keyboard)


@router.callback_query(F.data.startswith("al_del_confirm:"))
async def confirm_soft_delete_alert(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Exécute la suppression définitive et complète de l'alerte et de ses données associées."""
    await callback.answer()
    alert_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore

    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        if isinstance(callback.message, Message):
            await callback.message.edit_text("Alerte déjà supprimée.")
        return

    alert_name = alert.name
    await alert_repo.delete_completely(alert)
    await db_session.commit()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"🗑 L'alerte « <b>{alert_name}</b> » et toutes ses données associées ont été supprimées définitivement.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📁 Retour aux catégories", callback_data="ed_cat_menu"
                        )
                    ]
                ]
            ),
        )

