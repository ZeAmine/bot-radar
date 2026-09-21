"""Flux de création et d'ajout d'alertes (/add, /addisbn, /addurl)."""

import contextlib
import html
import logging
import urllib.parse
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers.alerts_helpers import (
    format_semi_auto_recap,
    process_bd_query_lookup,
    process_tech_or_clothes_query,
)
from app.bot.keyboards.alerts import (
    get_alert_confirmation_keyboard,
    get_category_selection_keyboard,
    get_conditions_toggle_keyboard,
)
from app.bot.states.alert_creation import AlertCreationState
from app.core.constants import (
    CATEGORY_EMOJIS,
    CATEGORY_LABELS,
    DEFAULT_BD_CONDITIONS,
    DEFAULT_CLOTHES_CONDITIONS,
    DEFAULT_GAMING_CONDITIONS,
    DEFAULT_TECH_CONDITIONS,
    STANDARD_CONDITIONS,
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
from app.services.ai.modules.natural_add import natural_add_module
from app.services.alert_service import (
    AlertServiceError,
    parse_comma_separated_list,
    parse_price_to_cents,
)
from app.utils.isbn import extract_clean_isbn
from app.utils.text import normalize_text

logger = logging.getLogger(__name__)

router = Router(name="alerts_create")


def _get_alert_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "AlertRepository", AlertRepository)(session)


def _get_category_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "CategoryRepository", CategoryRepository)(session)


def _get_book_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "BookRepository", BookRepository)(session)


def _get_market_price_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "MarketPriceRepository", MarketPriceRepository)(session)


@router.message(Command("add", "ajouter"))
@router.message(Command("addisbn"))
@router.message(Command("addurl"))
@router.callback_query(F.data == "btn_add_alert")
async def start_add_alert(
    event: Message | CallbackQuery,
    state: FSMContext,
    db_session: Any = None,
    db_user: User | None = None,
) -> None:
    """Point d'entrée unifié pour l'ajout d'alertes multi-catégories."""
    await state.clear()

    inline_query: str | None = None
    if isinstance(event, Message):
        raw_text = (event.text or "").strip()
        parts = raw_text.split(maxsplit=1)
        if len(parts) > 1:
            inline_query = parts[1].strip()

    if inline_query and natural_add_module.is_enabled:
        # Tenter le parsing en langage naturel via DeepSeek-V4.1-Flash
        parsed = await natural_add_module.parse_prompt(inline_query)
        if parsed:
            state_data = {
                "name": parsed.name,
                "category_slug": parsed.category_slug,
                "isbn": parsed.isbn13,
                "is_series": parsed.is_series,
                "author": parsed.author,
                "publisher": parsed.publisher,
                "include_keywords": parsed.include_keywords or [parsed.name],
                "exclude_keywords": parsed.exclude_keywords or [],
                "max_item_price_cents": parsed.max_price_cents,
                "selected_conditions": parsed.conditions,
            }
            await state.update_data(**state_data)
            await state.set_state(AlertCreationState.confirm_creation)

            recap = format_semi_auto_recap(
                title=parsed.name,
                author=parsed.author,
                publisher=parsed.publisher,
                isbn=parsed.isbn13,
                max_price_cents=parsed.max_price_cents,
                include_keywords=parsed.include_keywords or [parsed.name],
                exclude_keywords=parsed.exclude_keywords or [],
                category_slug=parsed.category_slug,
                conditions=parsed.conditions,
                is_series=parsed.is_series,
            )
            prompt_text = (
                "🪄 <b>Alerte configurée en langage naturel !</b>\n\n"
                f"{recap}\n\n"
                "<i>Clique sur « Créer l'alerte » pour valider ou « Modifier » pour ajuster.</i>"
            )
            keyboard = get_alert_confirmation_keyboard()
            if isinstance(event, CallbackQuery):
                await event.answer()
                if event.message:
                    await event.message.answer(prompt_text, reply_markup=keyboard)
            else:
                await event.answer(prompt_text, reply_markup=keyboard)
            return

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
            await process_bd_query_lookup(
                callback, state, inline_query, db_session, db_user=db_user
            )
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
            "<i>(ex: Raspberry Pi 4, Steam Deck 512Go, Sony WH-1000XM4...)\n\n"
            "<i>(Tape /cancel à tout moment pour annuler)</i>"
        )
    elif category_slug == CategorySlug.GAMING.value:
        prompt = (
            "🎮 <b>Alerte Jeux Vidéo</b>\n\n"
            "Quel jeu ou console recherches-tu ?\n"
            "<i>(ex: Mario Kart 8 Switch, Zelda Tears of the Kingdom, Switch Oled...)\n\n"
            "<i>(Tape /cancel à tout moment pour annuler)</i>"
        )
    else:
        prompt = (
            "👕 <b>Alerte Vêtements</b>\n\n"
            "Quel vêtement ou modèle recherches-tu ?\n"
            "<i>(ex: Veste Carhartt Detroit, Pull Ralph Lauren M...)\n\n"
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

    alert_repo = _get_alert_repo(db_session)
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
        alert_repo = _get_alert_repo(db_session)
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
        variants=data.get("variants"),
    )
    await message.answer(recap_text, reply_markup=get_alert_confirmation_keyboard())


@router.callback_query(AlertCreationState.confirm_creation, F.data == "alert_create_ai_variants")
async def handle_create_ai_variants(callback: CallbackQuery, state: FSMContext) -> None:
    """Génère et ajoute les variantes orthographiques et exclusions au brouillon d'alerte."""
    data = await state.get_data()
    name = data.get("name") or data.get("inline_query") or ""
    if not name:
        await callback.answer("Aucun titre disponible pour générer des variantes.", show_alert=True)
        return

    category_slug = data.get("category_slug", CategorySlug.BD_COMICS.value)
    await callback.answer("🤖 Recherche des variantes et exclusions IA...")
    from app.services.ai.modules.keyword_variants import keyword_variants_module

    variants, new_exclusions = await keyword_variants_module.generate_variants_and_exclusions(
        name, category_slug=category_slug
    )
    if not variants and not new_exclusions:
        await callback.answer("Aucune variante ou exclusion spécifique trouvée.", show_alert=True)
        return

    # Stocker les variantes dans 'variants' sans polluer 'include_keywords'
    existing_variants = list(data.get("variants") or [])
    for v in variants:
        if v.lower() not in [x.lower() for x in existing_variants]:
            existing_variants.append(v)

    # Fusionner les exclusions sans doublons
    existing_exclusions = list(data.get("exclude_keywords") or [])
    for e in new_exclusions:
        if e.lower() not in [x.lower() for x in existing_exclusions]:
            existing_exclusions.append(e)

    await state.update_data(variants=existing_variants, exclude_keywords=existing_exclusions)

    include_keywords = data.get("include_keywords") or [name]
    max_price_cents = data.get("max_item_price_cents")
    conditions = data.get("selected_conditions") or []

    recap = format_semi_auto_recap(
        title=name,
        author=data.get("author"),
        publisher=data.get("publisher"),
        isbn=data.get("isbn"),
        max_price_cents=max_price_cents,
        include_keywords=include_keywords,
        exclude_keywords=existing_exclusions,
        category_slug=category_slug,
        conditions=conditions,
        variants=existing_variants,
    )
    prompt_text = (
        "🪄 <b>Alerte mise à jour avec les optimisations IA !</b>\n\n"
        f"{recap}\n\n"
        "<i>Clique sur « Créer l'alerte » pour valider ou « Modifier » pour ajuster.</i>"
    )
    keyboard = get_alert_confirmation_keyboard(show_variants_btn=False)
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(prompt_text, reply_markup=keyboard)


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
    category_repo = _get_category_repo(db_session)
    category = None
    if cat_id_str:
        with contextlib.suppress(Exception):
            category = await category_repo.get_by_id(uuid.UUID(cat_id_str))
    if not category:
        category = await category_repo.get_by_slug(category_slug)

    alert_repo = _get_alert_repo(db_session)

    if category_slug == CategorySlug.BD_COMICS.value:
        book_repo = _get_book_repo(db_session)
        book = None
        best_offer = None
        hist_low = None

        if isbn:
            # 1. One-Shot ou tome spécifique : Persistance définitive de l'ISBN en base
            book = await book_repo.get_by_isbn13(isbn)
            if not book:
                book = await book_repo.create(
                    title=name,
                    normalized_title=normalize_text(name),
                    isbn13=isbn,
                    author=author,
                    publisher=publisher,
                )

            # 2. Création de l'alerte liée à l'ISBN
            vinted_url = f"https://www.vinted.fr/catalog?search_start_type=item_page_isbn&search_text={isbn}&order=newest_first"
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

            # 3. Liaison alerte <-> livre de référence
            if book:
                await book_repo.link_alert_to_book(alert.id, book.id, MatchConfidence.HIGH)

            # 4. Préchauffage et enregistrement de la cote du marché EXCLUSIVEMENT par ISBN
            market_repo = _get_market_price_repo(db_session)
            try:
                provider = ChasseAuxLivresProvider()
                offers = await provider.get_offers(isbn13=isbn, title="")
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
                            best_provider_name=best_offer.raw_payload.get("best_merchant")
                            or best_offer.provider_name,
                            best_source_url=best_offer.source_url,
                            confidence=best_offer.confidence,
                            comparison_status=ComparisonStatus.AVAILABLE,
                            cache_duration_hours=24,
                            historical_low_1y_cents=hist_low,
                        )
            except Exception as exc:
                logger.warning("Erreur Chasse-aux-livres par ISBN lors de la création d'alerte : %s", exc)
        else:
            # Série multi-tomes : Création propre de l'alerte sans faux livre unique ni fausse cote
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

        price_str = f"{max_price / 100:.2f} €".replace(".", ",") if max_price else "Aucun"
        cond_str = ", ".join(conditions) if conditions else "Neuf avec étiquette, Neuf sans étiquette, Très bon état"

        lines = [
            "✅ <b>Alerte créée avec succès !</b>\n",
            f"📚 <b>{html.escape(name)}</b>",
        ]
        if isbn:
            lines.append(f"🔢 <b>ISBN :</b> <code>{isbn}</code>")

        lines.append(f"🎯 <b>Prix max :</b> {price_str}")
        lines.append(f"🔑 <b>Inclus :</b> {', '.join(includes)}")
        if variants:
            lines.append(f"✨ <b>Variantes :</b> {', '.join(variants)}")
        if excludes:
            lines.append(f"🚫 <b>Exclus :</b> {', '.join(excludes)}")

        lines.append(f"🏷 <b>États :</b> {cond_str}\n")

        if best_offer and best_offer.delivered_price_cents:
            lowest_str = f"{best_offer.delivered_price_cents / 100:.2f} € ({best_offer.provider_name})".replace(".", ",")
            lines.append(f"📊 <b>Prix du marché :</b> {lowest_str}")
            if hist_low:
                hist_low_str = f"{hist_low / 100:.2f} €".replace(".", ",")
                lines.append(f"📉 <b>Prix bas constaté (1 an) :</b> {hist_low_str}")
            lines.append("")

        lines.append(
            "⚡ <i>Surveillance active 24h/24 sur Vinted. "
            "Vous recevrez une notification instantanée dès qu'une offre éligible apparaît.</i>"
        )
        resp_text = "\n".join(lines)
    else:
        # Tech ou Vêtements (sans livre ni cote de livres)
        vinted_url = (
            f"https://www.vinted.fr/catalog?search_text={urllib.parse.quote_plus(name)}"
            "&order=newest_first"
        )
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

        lines = [
            "✅ <b>Alerte créée avec succès !</b>\n",
            f"{cat_emoji} <b>{html.escape(name)}</b> ({cat_label})",
            f"🎯 <b>Prix max :</b> {price_str}",
            f"🔑 <b>Inclus :</b> {', '.join(includes)}",
        ]
        if variants:
            lines.append(f"✨ <b>Variantes :</b> {', '.join(variants)}")
        if excludes:
            lines.append(f"🚫 <b>Exclus :</b> {', '.join(excludes)}")
        lines.append(f"🏷 <b>États :</b> {cond_str}\n")
        lines.append(
            "⚡ <i>Surveillance active 24h/24 sur Vinted. "
            "Vous recevrez une notification instantanée dès qu'une offre éligible apparaît.</i>"
        )
        resp_text = "\n".join(lines)

    vinted_btn = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Voir sur Vinted", url=vinted_url)]
        ]
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(resp_text, reply_markup=vinted_btn)


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


@router.callback_query(
    F.data.in_(["alert_create_edit", "al_create_edit:menu"])
)
async def edit_alert_creation_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Affiche le menu complet de modification des informations d'alerte."""
    await callback.answer()
    data = await state.get_data()
    name = data.get("name", "cet article")
    category_slug = data.get("category_slug", CategorySlug.BD_COMICS.value)
    isbn = data.get("isbn")
    current_price = data.get("max_item_price_cents")
    current_price_str = (
        f"{current_price / 100:.2f} €".replace(".", ",") if current_price else "Aucun"
    )

    text = (
        f"✏️ <b>Modification de l'alerte « {html.escape(name)} »</b>\n\n"
        "Choisis l'information que tu souhaites modifier :"
    )

    buttons = [
        [InlineKeyboardButton(text="📌 Nom / Titre", callback_data="al_create_edit:name")],
    ]

    # Pour les BD/Comics ou si un ISBN est présent
    if category_slug == CategorySlug.BD_COMICS.value or isbn:
        isbn_str = f" ({isbn})" if isbn else " (aucun)"
        buttons.append([InlineKeyboardButton(text=f"🔢 ISBN{isbn_str}", callback_data="al_create_edit:isbn")])

    buttons.extend([
        [InlineKeyboardButton(text=f"🎯 Prix max ({current_price_str})", callback_data="al_create_edit:price")],
        [InlineKeyboardButton(text="🔑 Mots-clés inclus", callback_data="al_create_edit:include_keywords")],
        [InlineKeyboardButton(text="🚫 Mots-clés exclus", callback_data="al_create_edit:exclude_keywords")],
        [InlineKeyboardButton(text="✨ Variantes", callback_data="al_create_edit:variants")],
        [InlineKeyboardButton(text="🏷 États acceptés", callback_data="al_create_edit:conditions")],
        [InlineKeyboardButton(text="🔄 Changer de produit / Recommencer", callback_data="al_create_edit:restart")],
        [InlineKeyboardButton(text="← Retour au récapitulatif", callback_data="al_create_edit:back")],
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(text, reply_markup=keyboard)


async def prompt_new_price(callback: CallbackQuery, state: FSMContext) -> None:
    """Demande la saisie d'un nouveau montant (compatibilité)."""
    await handle_create_edit_choice(callback, state)


@router.callback_query(F.data.startswith("al_create_edit:"))
async def handle_create_edit_choice(callback: CallbackQuery, state: FSMContext) -> None:
    """Affiche l'invite de saisie pour le champ sélectionné lors de la création."""
    parts = callback.data.split(":")
    field = parts[1]

    if field == "back":
        await back_to_recap(callback, state)
        return
    if field == "restart":
        await restart_from_scratch(callback, state)
        return
    if field == "menu":
        await edit_alert_creation_prompt(callback, state)
        return
    if field == "conditions":
        await callback.answer()
        data = await state.get_data()
        selected = list(data.get("selected_conditions") or STANDARD_CONDITIONS)
        text = (
            "💎 <b>Choisis les états acceptés pour cette alerte :</b>\n\n"
            "Coche ou décoche les états souhaités puis clique sur Confirmer :"
        )
        keyboard = get_conditions_toggle_keyboard(selected)
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text="← Retour au menu", callback_data="al_create_edit:menu")
        ])
        if callback.message and hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(text, reply_markup=keyboard)
        return

    # Champs textuels
    await callback.answer()
    data = await state.get_data()
    await state.set_state(AlertCreationState.waiting_for_edit_field)
    await state.update_data(current_edit_field=field)

    field_labels = {
        "name": ("Nom / Titre", data.get("name", ""), "Envoie le nouveau nom / titre :"),
        "isbn": (
            "ISBN",
            data.get("isbn", "") or "",
            "Envoie le nouvel ISBN (10 ou 13 chiffres, ou /skip pour retirer) :",
        ),
        "price": (
            "Prix maximum (€)",
            f"{data.get('max_item_price_cents', 0) / 100:.2f}".replace(".", ",")
            if data.get("max_item_price_cents")
            else "",
            "Envoie le nouveau prix max en € (ex: 15 ou 25) :",
        ),
        "include_keywords": (
            "Mots-clés inclus",
            ", ".join(data.get("include_keywords", [])),
            "Envoie les mots-clés séparés par des virgules :",
        ),
        "exclude_keywords": (
            "Exclusions",
            ", ".join(data.get("exclude_keywords", [])),
            "Envoie les exclusions séparées par des virgules (ou /skip pour effacer) :",
        ),
        "variants": (
            "Variantes",
            ", ".join(data.get("variants", [])),
            "Envoie les variantes séparées par des virgules (ou /skip pour effacer) :",
        ),
    }

    title, current_val, hint = field_labels.get(
        field, (field, "", "Envoie la nouvelle valeur :")
    )

    if current_val:
        text = (
            f"🎯 <b>{title} actuel :</b> <i>(touche pour copier)</i>\n\n"
            f"<code>{html.escape(current_val)}</code>\n\n"
            f"<i>{hint}</i>"
        )
    else:
        text = (
            f"🎯 <b>{title} :</b> <i>(actuellement vide)</i>\n\n"
            f"<i>{hint}</i>"
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Retour au menu", callback_data="al_create_edit:menu")],
            [InlineKeyboardButton(text="← Retour au récapitulatif", callback_data="al_create_edit:back")],
        ]
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("cond_toggle:"))
async def handle_creation_condition_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    """Bascule l'état d'une condition pendant la création d'alerte."""
    parts = callback.data.split(":")
    idx = int(parts[1])
    target_cond = STANDARD_CONDITIONS[idx]
    data = await state.get_data()
    selected = list(data.get("selected_conditions") or STANDARD_CONDITIONS)
    if target_cond in selected:
        selected.remove(target_cond)
    else:
        selected.append(target_cond)
    await state.update_data(selected_conditions=selected)
    await callback.answer()
    keyboard = get_conditions_toggle_keyboard(selected)
    keyboard.inline_keyboard.append([
        InlineKeyboardButton(text="← Retour au menu", callback_data="al_create_edit:menu")
    ])
    if callback.message and hasattr(callback.message, "edit_reply_markup"):
        await callback.message.edit_reply_markup(reply_markup=keyboard)


@router.callback_query(F.data == "cond_confirm")
async def handle_creation_condition_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Confirme les conditions et retourne au récapitulatif."""
    await callback.answer("États mis à jour !")
    await back_to_recap(callback, state)


@router.message(AlertCreationState.waiting_for_edit_field)
async def process_creation_edit_input(message: Message, state: FSMContext) -> None:
    """Traite la saisie textuelle pour le champ en cours de modification à la création."""
    data = await state.get_data()
    field = data.get("current_edit_field")
    raw_text = (message.text or "").strip()

    if field == "name":
        if len(raw_text) < 2:
            await message.answer("⚠️ Le nom doit comporter au moins 2 caractères. Réessaie :")
            return
        await state.update_data(name=raw_text)

    elif field == "isbn":
        if raw_text.lower() in ("/skip", "-", "aucun", "aucune", "clear"):
            await state.update_data(isbn=None, is_series=True)
        else:
            clean_isbn = extract_clean_isbn(raw_text)
            if not clean_isbn:
                await message.answer(
                    "⚠️ Format d'ISBN invalide (doit comporter 10 ou 13 chiffres). Réessaie (ou /skip) :"
                )
                return
            await state.update_data(isbn=clean_isbn, is_series=False)
            try:
                from app.services.book_metadata_service import BookMetadataService
                meta = await BookMetadataService().lookup_by_isbn(clean_isbn)
                if meta:
                    updates = {}
                    if meta.author:
                        updates["author"] = meta.author
                    if meta.publisher:
                        updates["publisher"] = meta.publisher
                    if meta.title and (not data.get("name") or len(data.get("name")) < 3):
                        updates["name"] = meta.title
                    if updates:
                        await state.update_data(**updates)
            except Exception:
                pass

    elif field == "price":
        try:
            price_cents = parse_price_to_cents(raw_text)
            await state.update_data(max_item_price_cents=price_cents)
        except AlertServiceError as exc:
            await message.answer(f"⚠️ {exc}\nMerci d'indiquer un montant valide (ex: 15 ou 25) :")
            return

    elif field == "include_keywords":
        parsed = parse_comma_separated_list(raw_text)
        if not parsed:
            await message.answer("⚠️ Au moins un mot-clé requis. Réessaie :")
            return
        await state.update_data(include_keywords=parsed)

    elif field == "exclude_keywords":
        if raw_text.lower() in ("/skip", "-", "aucun", "aucune"):
            await state.update_data(exclude_keywords=[])
        else:
            await state.update_data(exclude_keywords=parse_comma_separated_list(raw_text))

    elif field == "variants":
        if raw_text.lower() in ("/skip", "-", "aucun", "aucune"):
            await state.update_data(variants=[])
        else:
            await state.update_data(variants=parse_comma_separated_list(raw_text))

    # Réafficher le récapitulatif mis à jour
    updated_data = await state.get_data()
    await state.set_state(AlertCreationState.confirm_creation)

    recap_text = format_semi_auto_recap(
        title=updated_data.get("name", "Alerte"),
        author=updated_data.get("author"),
        publisher=updated_data.get("publisher"),
        isbn=updated_data.get("isbn"),
        max_price_cents=updated_data.get("max_item_price_cents"),
        include_keywords=updated_data.get("include_keywords", []),
        exclude_keywords=updated_data.get("exclude_keywords", []),
        category_slug=updated_data.get("category_slug", CategorySlug.BD_COMICS.value),
        conditions=updated_data.get("selected_conditions"),
        variants=updated_data.get("variants"),
        is_series=updated_data.get("is_series", False),
    )
    await message.answer(
        f"✅ <b>Modification enregistrée !</b>\n\n{recap_text}",
        reply_markup=get_alert_confirmation_keyboard(),
    )


@router.callback_query(F.data == "al_create_edit:back")
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
        variants=data.get("variants"),
        is_series=data.get("is_series", False),
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(
            recap_text, reply_markup=get_alert_confirmation_keyboard()
        )


@router.callback_query(F.data == "al_create_edit:restart")
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
