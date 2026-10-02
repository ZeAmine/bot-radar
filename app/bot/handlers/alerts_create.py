"""Flux de création et d'ajout d'alertes (/add, /addisbn, /addurl)."""

import contextlib
import html
import logging
import urllib.parse
import uuid
from typing import Any

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers._alerts_router import router
from app.bot.handlers.alerts_helpers import (
    creation_keyboard,
    process_bd_query_lookup,
    process_tech_or_clothes_query,
    recap_from_state,
)
from app.bot.keyboards.alerts import get_category_selection_keyboard
from app.bot.states.alert_creation import AlertCreationState
from app.core.constants import (
    CATEGORY_EMOJIS,
    CATEGORY_LABELS,
    DEFAULT_BD_CONDITIONS,
    DEFAULT_CLOTHES_CONDITIONS,
    DEFAULT_GAMING_CONDITIONS,
    DEFAULT_TECH_CONDITIONS,
    CategorySlug,
    ComparisonStatus,
    MatchConfidence,
)
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.category_repo import CategoryRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.providers.base import extract_best_historical_low
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
from app.providers.registry import ProviderRegistry
from app.services.ai.modules.keyword_variants import keyword_variants_module
from app.services.ai.modules.natural_add import natural_add_module
from app.services.alert_service import (
    AlertServiceError,
    parse_price_to_cents,
)
from app.utils.text import normalize_text, strip_html_tags

logger = logging.getLogger(__name__)


def _get_alert_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "AlertRepository", AlertRepository)(session)


def _merge_keywords(*groups: list[str] | None) -> list[str]:
    """Fusionne des listes de mots-clés en préservant l'ordre et en dédupliquant."""
    merged: list[str] = []
    for group in groups:
        for term in group or []:
            value = str(term).strip()
            if not value or any(value.lower() == kept.lower() for kept in merged):
                continue
            merged.append(value)
    return merged


async def _finalize_loading_message(
    loading_msg: Message | bool,
    event: Message | CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    """Édite le message d'attente si possible, sinon envoie une nouvelle réponse.

    Reste tolérant aux messages non éditables (réponse de callback renvoyant un bool).
    """
    try:
        edit_text = getattr(loading_msg, "edit_text", None)
        if callable(edit_text):
            await edit_text(text, reply_markup=keyboard)
            return
    except Exception:
        pass

    try:
        await event.answer(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        # Un handler qui lève vaut « update non traité » pour Telegram, qui rejoue
        # alors le même update : l'utilisateur voit « Recherche IA en cours » en
        # boucle. Un refus de formatage HTML est un défaut de rendu, pas une panne :
        # on le journalise et on renvoie le récap sans balisage.
        logger.warning("Réponse Telegram refusée (%s), renvoi du récap en texte brut.", exc)
        await event.answer(strip_html_tags(text), reply_markup=keyboard, parse_mode=None)


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
    _db_session: Any = None,
    _db_user: User | None = None,
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
        # Feedback visuel immédiat : indicateur de frappe + message d'attente
        if isinstance(event, Message) and event.bot:
            with contextlib.suppress(Exception):
                await event.bot.send_chat_action(chat_id=event.chat.id, action="typing")

        loading_text = (
            "🪄 <b>Création d'alerte en langage naturel</b>\n\n"
            "⏳ <i>L'IA analyse votre demande pour "
            f"« <b>{html.escape(inline_query)}</b> »...</i>"
        )
        loading_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⏳ Recherche IA en cours...", callback_data="noop")]
            ]
        )
        loading_msg = await event.answer(loading_text, reply_markup=loading_kb)

        # Parsing en langage naturel par le moteur IA configuré
        parsed = await natural_add_module.parse_prompt(inline_query)
        if parsed:
            # Mots-clés ancrés sur la fiche réelle de l'ouvrage (BNF quand elle existe)
            ai_variants, ai_exclusions = (
                await keyword_variants_module.generate_variants_and_exclusions(
                    parsed.name,
                    category_slug=parsed.category_slug,
                    context=parsed.book_context,
                )
            )
            include_keywords = parsed.include_keywords or [parsed.name]
            exclude_keywords = _merge_keywords(parsed.exclude_keywords, ai_exclusions)
            variants = ai_variants

            state_data = {
                "name": parsed.name,
                "category_slug": parsed.category_slug,
                "isbn": parsed.isbn13,
                "is_series": parsed.is_series,
                "author": parsed.author,
                "publisher": parsed.publisher,
                "include_keywords": include_keywords,
                "exclude_keywords": exclude_keywords,
                "variants": variants,
                "max_item_price_cents": parsed.max_price_cents,
                "selected_conditions": parsed.conditions,
                "isbn_note": parsed.isbn_note,
                "book_context": (
                    parsed.book_context.model_dump() if parsed.book_context else None
                ),
                "edition_alternatives": [
                    edition.model_dump() for edition in parsed.edition_alternatives
                ],
            }
            await state.update_data(data=state_data)
            await state.set_state(AlertCreationState.confirm_creation)

            recap = recap_from_state(state_data)
            prompt_text = (
                "🪄 <b>Alerte configurée en langage naturel !</b>\n\n"
                f"{recap}\n\n"
                "<i>Clique sur « Créer l'alerte » pour valider ou « Modifier » pour ajuster.</i>"
            )
            keyboard = creation_keyboard(state_data)
            await _finalize_loading_message(loading_msg, event, prompt_text, keyboard)
            return

        # Fallback si l'IA n'a pas pu structurer la demande : sélection de catégorie
        await state.update_data(inline_query=inline_query)
        prompt_text = (
            f"🎯 <b>Ajout d'une alerte pour « {html.escape(inline_query)} »</b>\n\n"
            "Choisis la catégorie de ce produit :"
        )
        await state.set_state(AlertCreationState.waiting_for_category)
        keyboard = get_category_selection_keyboard()
        await _finalize_loading_message(loading_msg, event, prompt_text, keyboard)
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

    updated_data = {**data, "max_item_price_cents": price_cents}
    await message.answer(
        recap_from_state(updated_data),
        reply_markup=creation_keyboard(updated_data),
    )


@router.callback_query(AlertCreationState.confirm_creation, F.data == "alert_create_ai_variants")
async def handle_create_ai_variants(callback: CallbackQuery, state: FSMContext) -> None:
    """Génère et ajoute les variantes orthographiques et exclusions au brouillon d'alerte."""
    data = await state.get_data()
    name = data.get("name") or data.get("inline_query") or ""
    if not name:
        await callback.answer("Aucun titre disponible pour générer des variantes.", show_alert=True)
        return

    category_slug = data.get("category_slug", CategorySlug.BD_COMICS.value)
    await callback.answer()

    loading_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Recherche IA en cours...", callback_data="noop")]
        ]
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(
            f"⏳ <b>Recherche des variantes et exclusions IA...</b>\n\n"
            f"Analyse de « <b>{html.escape(name)}</b> » en cours.",
            reply_markup=loading_kb,
        )

    from app.services.ai.schemas import BookContext

    book_context = (
        BookContext(**data["book_context"]) if data.get("book_context") else None
    )
    variants, new_exclusions = await keyword_variants_module.generate_variants_and_exclusions(
        name, category_slug=category_slug, context=book_context
    )

    if not variants and not new_exclusions:
        recap = recap_from_state(data)
        prompt_text = (
            "ℹ️ <b>Aucune variante ou exclusion supplémentaire nécessaire.</b>\n\n"
            f"{recap}\n\n"
            "<i>Clique sur « Créer l'alerte » pour valider ou « Modifier » pour ajuster.</i>"
        )
        keyboard = creation_keyboard(data, show_variants_btn=False)
        if callback.message and hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(prompt_text, reply_markup=keyboard)
        return

    # Stocker les variantes dans 'variants' sans polluer 'include_keywords'
    existing_variants = _merge_keywords(data.get("variants"), variants)

    # Fusionner les exclusions sans doublons
    existing_exclusions = _merge_keywords(data.get("exclude_keywords"), new_exclusions)

    await state.update_data(variants=existing_variants, exclude_keywords=existing_exclusions)

    updated_draft = {
        **data,
        "variants": existing_variants,
        "exclude_keywords": existing_exclusions,
    }
    recap = recap_from_state(updated_draft)
    prompt_text = (
        "🪄 <b>Alerte mise à jour avec les optimisations IA !</b>\n\n"
        f"{recap}\n\n"
        "<i>Clique sur « Créer l'alerte » pour valider ou « Modifier » pour ajuster.</i>"
    )
    keyboard = creation_keyboard(updated_draft, show_variants_btn=False)
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
                        hist_low = extract_best_historical_low([best_offer])
                        merchant = (
                            best_offer.raw_payload.get("best_merchant")
                            if best_offer.raw_payload
                            else None
                        )
                        await market_repo.update_or_create_cache(
                            book_id=book.id,
                            lowest_delivered_price_cents=best_offer.delivered_price_cents,
                            best_provider_name=merchant or best_offer.provider_name,
                            best_source_url=best_offer.source_url,
                            confidence=best_offer.confidence,
                            comparison_status=ComparisonStatus.AVAILABLE,
                            cache_duration_hours=24,
                            historical_low_1y_cents=hist_low,
                        )
            except Exception as exc:
                logger.warning(
                    "Erreur Chasse-aux-livres par ISBN lors de la création d'alerte : %s", exc
                )
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
        default_conds = "Neuf avec étiquette, Neuf sans étiquette, Très bon état"
        cond_str = ", ".join(conditions) if conditions else default_conds

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
            deliv_cents = best_offer.delivered_price_cents
            prov = best_offer.provider_name
            lowest_str = f"{deliv_cents / 100:.2f} € ({prov})".replace(".", ",")
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
