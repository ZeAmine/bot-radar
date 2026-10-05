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
    _join_escaped,
    build_creation_recap,
    extract_inline_price_and_clean_query,
    process_bd_query_lookup,
    process_tech_or_clothes_query,
    render_existing_alert_response,
)
from app.bot.keyboards.alerts import get_category_selection_keyboard
from app.bot.labels import format_conditions, format_price_cents
from app.bot.states.alert_creation import AlertCreationState
from app.core.constants import (
    CATEGORY_EMOJIS,
    CATEGORY_LABELS,
    DEFAULT_BD_CONDITIONS,
    DEFAULT_CLOTHES_CONDITIONS,
    DEFAULT_GAMING_CONDITIONS,
    DEFAULT_TECH_CONDITIONS,
    CategorySlug,
    MatchConfidence,
)
from app.core.tasks import safe_create_task
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.category_repo import CategoryRepository
from app.services.ai.modules.keyword_variants import (
    KeywordVariantsOutcome,
    keyword_variants_module,
)
from app.services.ai.modules.natural_add import natural_add_module
from app.services.ai.schemas import BookContext
from app.services.alert_service import (
    AlertServiceError,
    parse_price_to_cents,
)
from app.services.book_market_pricing import schedule_book_pricing
from app.services.market_price_service import (
    PENDING_COTE_LINE,
    format_cote_lines,
)
from app.utils.isbn import extract_clean_isbn
from app.utils.text import normalize_text, strip_html_tags

logger = logging.getLogger(__name__)

# Pied de la confirmation de création, identique pour toutes les catégories. Une seule
# formulation : les deux branches (livre et produit) annoncent la même chose, donc elles
# ne peuvent pas diverger. Le pied décrit le mécanisme réel — une annonce éligible
# déclenche une alerte au prochain passage du poller — sans promettre d'instantanéité
# ni de surveillance permanente.
_CONFIRMATION_FOOTER = (
    "<i>Les annonces correspondant à ces critères déclencheront une alerte Telegram.</i>"
)


def _get_alert_repo(session: Any) -> Any:
    import unittest.mock

    import app.bot.handlers.alerts as alerts_mod

    if isinstance(AlertRepository, unittest.mock.Base):
        return AlertRepository(session)
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


async def _show_step(
    loading_msg: Message | bool,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    """Fait avancer le jalon affiché, sans jamais rien bloquer.

    Contrairement à `_finalize_loading_message`, un échec est silencieux : un jalon
    est une information de confort, pas un résultat. On n'envoie surtout pas de second
    message de repli — l'utilisateur verrait deux messages au lieu d'un qui progresse.
    """
    edit_text = getattr(loading_msg, "edit_text", None)
    if not callable(edit_text):
        return
    try:
        await edit_text(text, reply_markup=keyboard)
    except Exception as exc:
        logger.debug("Jalon non affiché : %s", exc)


def _draft_matches(current: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    """Le brouillon affiché est-il encore celui sur lequel on travaille ?

    La recherche des variantes part en arrière-plan : entre le jalon et la réponse du
    moteur, l'utilisateur a pu annuler, modifier un champ ou relancer une commande.
    Écrire le récap dans ces cas reviendrait à afficher des mots-clés qui ne
    correspondent plus à l'état, ou à ressusciter une fiche abandonnée.

    L'ISBN et l'auteur en font partie : changer d'ouvrage change la fiche sur laquelle
    le moteur raisonne, et ses mots-clés seraient alors ceux d'un autre livre.
    """
    if not current:
        return False
    return all(
        current.get(key) == snapshot.get(key)
        for key in ("name", "category_slug", "isbn", "author")
    )


def relaunch_keyword_search(
    *,
    event: Message | CallbackQuery,
    loading_msg: Message | bool,
    state: FSMContext,
    draft: dict[str, Any],
) -> None:
    """Relance la recherche de mots-clés après un changement d'identité.

    Le même chemin que le départ de `/add` : la tâche part en arrière-plan, le récap est
    publié tout de suite sans les mots-clés, puis complété sur place. Les variantes et
    exclusions de l'ouvrage précédent ne survivent pas à son remplacement.
    """
    book_context = (
        BookContext(**draft["book_context"]) if draft.get("book_context") else None
    )
    safe_create_task(
        _search_keywords_in_background(
            loading_msg=loading_msg,
            event=event,
            state=state,
            snapshot=draft,
            name=str(draft.get("name") or ""),
            category_slug=str(draft.get("category_slug") or CategorySlug.BD_COMICS.value),
            book_context=book_context,
        ),
        name="add-variantes-realignement",
    )


def _keyword_note(outcome: KeywordVariantsOutcome) -> str | None:
    """Ligne discrète expliquant ce que la recherche n'a pas trouvé.

    Cinq fins possibles, cinq libellés distincts : le récap ne doit jamais laisser
    croire qu'une liste vide est un avis du moteur alors que personne n'a été interrogé
    — ni qu'une exclusion manquante est un oubli quand elle est une décision.

    Le texte est rendu en italique par `format_semi_auto_recap` (charte, règle 3) :
    c'est un avertissement, jamais une information du livre. La mise en forme vit
    donc dans le rendu, pas ici : deux notes ne peuvent pas porter deux styles.
    """
    if not outcome.answered:
        # La relance a déjà eu lieu : ce n'est plus de la lenteur mais un échec. Le
        # bouton reste sous la carte, c'est donc lui qu'on nomme, pas « Modifier ».
        return (
            "⚠️ L'IA n'a pas répondu après une relance : cette alerte sera créée "
            "sans variante ni exclusion. Appuie sur « ✨ Variantes & Exclusions IA » "
            "pour réessayer."
        )
    if outcome.is_empty:
        return (
            "🔎 L'IA n'a trouvé aucune variante ni exclusion pertinente : "
            "rien n'a été ajouté."
        )
    if not outcome.exclusions:
        return "🔎 L'IA n'a trouvé aucune exclusion pertinente pour cet ouvrage."
    if not outcome.variants:
        return "🔎 L'IA n'a trouvé aucune variante de frappe pour cet ouvrage."
    return None


async def _search_keywords_in_background(
    *,
    loading_msg: Message | bool,
    event: Message | CallbackQuery,
    state: FSMContext,
    snapshot: dict[str, Any],
    name: str,
    category_slug: str,
    book_context: Any,
) -> None:
    """Cherche les variantes et exclusions hors du chemin de l'utilisateur.

    Le jalon « ouvrage trouvé » est déjà parti : l'utilisateur n'attend rien. Quand la
    réponse arrive — ou quand elle ne vient pas — le récap est publié, avec ou sans
    les mots-clés trouvés, et en disant lequel des deux cas s'est produit.
    """
    outcome = await keyword_variants_module.generate_variants_and_exclusions(
        name,
        category_slug=category_slug,
        context=book_context,
    )

    if not _draft_matches(await state.get_data(), snapshot):
        logger.info("Brouillon modifié pendant la recherche IA de « %s » : récap non publié.", name)
        return

    # La recherche n'est « définitive » que si le moteur a répondu. Sinon le bouton
    # « ✨ Variantes & Exclusions IA » reste sous la carte, et un seul appui
    # relance : un échec ne doit pas se payer d'un détour par « Modifier ».
    finalized = outcome.answered

    updated = {
        **snapshot,
        "variants": outcome.variants,
        "exclude_keywords": _merge_keywords(
            snapshot.get("exclude_keywords"), outcome.exclusions
        ),
        # La note et l'état de la recherche voyagent avec le brouillon : changer
        # d'édition ou de prix plus tard ne doit pas les perdre au re-rendu.
        "keyword_note": _keyword_note(outcome),
        "keywords_finalized": finalized,
    }
    await state.update_data(
        variants=updated["variants"],
        exclude_keywords=updated["exclude_keywords"],
        keyword_note=updated["keyword_note"],
        keywords_finalized=finalized,
    )

    recap, keyboard = build_creation_recap(updated)
    await _finalize_loading_message(loading_msg, event, recap, keyboard)


def _get_category_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "CategoryRepository", CategoryRepository)(session)


def _get_book_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "BookRepository", BookRepository)(session)


def _schedule_series_resolution(
    *, alert_id: uuid.UUID, name: str, author: str | None
) -> None:
    """Déclenche la résolution d'une série sans ISBN, dès sa création.

    Une série surveillée par son nom n'a aucune fiche tant qu'aucun tome n'a été
    identifié : sa première annonce partait donc sans ligne de cote. La résolution
    part par le **nom de l'alerte**, demande à la BNF les notices de la série
    (`series=True`) et cote chaque tome lié — la prochaine annonce aura de quoi comparer.
    """
    from app.use_cases.ingest_listing import link_books_from_bnf

    safe_create_task(
        link_books_from_bnf(name, alert_id, author=author, series=True),
        name=f"bnf_link_{alert_id}",
    )


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

    if inline_query and db_user and db_session:
        # Étape 1 : Pré-vérification immédiate en base de données (zéro IA, zéro réseau)
        clean_query, inline_price_cents = extract_inline_price_and_clean_query(inline_query)
        isbn_candidate = extract_clean_isbn(clean_query)
        alert_repo = _get_alert_repo(db_session)
        existing = await alert_repo.find_existing(
            db_user.id,
            isbn=isbn_candidate,
            title=clean_query,
        )
        if existing:
            await render_existing_alert_response(
                event,
                state,
                existing,
                new_price_cents=inline_price_cents,
            )
            return

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
            # Étape 2 : Post-vérification après identification de l'œuvre
            if db_user and db_session:
                alert_repo = _get_alert_repo(db_session)
                existing = await alert_repo.find_existing(
                    db_user.id,
                    isbn=parsed.isbn13,
                    title=parsed.name,
                )
                if existing:
                    # Binaire : l'alerte existe déjà -> arrêt immédiat sans récap ni variante IA
                    await render_existing_alert_response(
                        event,
                        state,
                        existing,
                        new_price_cents=parsed.max_price_cents,
                        target_msg=loading_msg,
                    )
                    return

            state_data = {
                "name": parsed.name,
                "category_slug": parsed.category_slug,
                "isbn": parsed.isbn13,
                "is_series": parsed.is_series,
                "author": parsed.author,
                "publisher": parsed.publisher,
                "include_keywords": parsed.include_keywords or [parsed.name],
                "exclude_keywords": parsed.exclude_keywords,
                "variants": [],
                "max_item_price_cents": parsed.max_price_cents,
                "selected_conditions": parsed.conditions,
                "isbn_note": parsed.isbn_note,
                "book_context": (
                    parsed.book_context.model_dump() if parsed.book_context else None
                ),
                "edition_alternatives": [
                    edition.model_dump() for edition in parsed.edition_alternatives
                ],
                # Un titre homonyme ne propose que des œuvres à choisir : sans elles,
                # l'avertissement du récap n'aurait rien derrière quoi se trancher.
                "work_choices": [
                    work.model_dump() for work in parsed.work_choices
                ],
            }
            await state.update_data(data=state_data)
            await state.set_state(AlertCreationState.confirm_creation)

            # Jalon : l'ouvrage est identifié et la suite part en arrière-plan. Ce
            # message est un simple statut — il ne doit surtout pas attendre la
            # réponse du moteur, ni porter un clavier qui validerait une alerte dont
            # les mots-clés sont encore en cours.
            found_details = " · ".join(
                html.escape(bit) for bit in (parsed.author, parsed.publisher) if bit
            )
            step_text = f"🔎 <b>{html.escape(parsed.name)}</b> identifié"
            if found_details:
                step_text += f"\n\n<i>{found_details}</i>"

            step_text += (
                "\n\n⏳ <i>Recherche des variantes de frappe et des exclusions "
                "propres à ce produit...</i>"
            )
            await _show_step(
                loading_msg,
                step_text,
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🔍 Variantes & exclusions en cours...",
                                callback_data="noop",
                            )
                        ]
                    ]
                ),
            )

            safe_create_task(
                _search_keywords_in_background(
                    loading_msg=loading_msg,
                    event=event,
                    state=state,
                    snapshot=state_data,
                    name=parsed.name,
                    category_slug=parsed.category_slug,
                    book_context=parsed.book_context,
                ),
                name="add-variantes",
            )
            return

        # Fallback si l'IA n'a pas pu structurer la demande : sélection de catégorie
        await state.update_data(inline_query=inline_query)
        prompt_text = (
            f"➕ <b>Créer une alerte</b>\n\n"
            f"<b>Article :</b> « {html.escape(inline_query)} »\n"
            "<i>Choisis la catégorie de ce produit.</i>"
        )
        await state.set_state(AlertCreationState.waiting_for_category)
        keyboard = get_category_selection_keyboard()
        await _finalize_loading_message(loading_msg, event, prompt_text, keyboard)
        return

    if inline_query:
        await state.update_data(inline_query=inline_query)
        prompt_text = (
            f"➕ <b>Créer une alerte</b>\n\n"
            f"<b>Article :</b> « {html.escape(inline_query)} »\n"
            "<i>Choisis la catégorie de ce produit.</i>"
        )
    else:
        prompt_text = (
            "➕ <b>Créer une alerte</b>\n\n"
            "<i>Choisis la catégorie du produit à surveiller.</i>"
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
            "<i>Entre le titre de l'œuvre que tu souhaites surveiller "
            "(avec auteur optionnel).</i>\n\n"
            "<i>(Tape /cancel à tout moment pour annuler)</i>"
        )
    elif category_slug == CategorySlug.TECH.value:
        prompt = (
            "💻 <b>Alerte Électronique</b>\n\n"
            "<i>Quel produit ou modèle électronique recherches-tu ?</i>\n\n"
            "<i>(ex: Raspberry Pi 4, Steam Deck 512Go, Sony WH-1000XM4...)</i>\n\n"
            "<i>(Tape /cancel à tout moment pour annuler)</i>"
        )
    elif category_slug == CategorySlug.GAMING.value:
        prompt = (
            "🎮 <b>Alerte Jeux Vidéo</b>\n\n"
            "<i>Quel jeu ou console recherches-tu ?</i>\n\n"
            "<i>(ex: Mario Kart 8 Switch, Zelda Tears of the Kingdom, Switch Oled...)</i>\n\n"
            "<i>(Tape /cancel à tout moment pour annuler)</i>"
        )
    else:
        prompt = (
            "👕 <b>Alerte Vêtements</b>\n\n"
            "<i>Quel vêtement ou modèle recherches-tu ?</i>\n\n"
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
        await message.answer(
            "<i>La recherche doit contenir au moins 2 caractères. Réessaie :</i>"
        )
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
        new_price_str = format_price_cents(price_cents)
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"✅ <b>Prix maximum mis à jour</b>\n\n"
                f"📚 <b>{html.escape(existing.name)}</b>\n"
                f"<b>Nouveau prix maximum :</b> {new_price_str}\n\n"
                "L'alerte existante a été actualisée sans créer de doublon."
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
            new_price_str = format_price_cents(price_cents)
            await message.answer(
                f"✅ <b>Prix maximum mis à jour</b>\n\n"
                f"📚 <b>{html.escape(existing.name)}</b>\n"
                f"<b>Nouveau prix maximum :</b> {new_price_str}\n\n"
                "L'alerte existante a été actualisée sans créer de doublon."
            )
            return

    await state.update_data(max_item_price_cents=price_cents)
    await state.set_state(AlertCreationState.confirm_creation)

    updated_data = {**data, "max_item_price_cents": price_cents}
    recap_text, keyboard = build_creation_recap(updated_data)
    await message.answer(recap_text, reply_markup=keyboard)


@router.callback_query(AlertCreationState.confirm_creation, F.data == "alert_create_ai_variants")
async def handle_create_ai_variants(callback: CallbackQuery, state: FSMContext) -> None:
    """Génère et ajoute les variantes orthographiques et exclusions au brouillon d'alerte."""
    data = await state.get_data()
    name = data.get("name") or data.get("inline_query") or ""
    if not name:
        await callback.answer("Aucun titre disponible pour générer des variantes.", show_alert=True)
        return

    category_slug = data.get("category_slug", CategorySlug.BD_COMICS.value)
    # Réponse immédiate : le spinner du bouton est levé avant l'appel. Répondre
    # après le moteur dépasserait la fenêtre de Telegram, qui renverrait « query is
    # too old » et rejouerait l'update. Le verdict de la recherche est dans la carte.
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

    book_context = (
        BookContext(**data["book_context"]) if data.get("book_context") else None
    )
    outcome = await keyword_variants_module.generate_variants_and_exclusions(
        name, category_slug=category_slug, context=book_context
    )

    # « Rien de pertinent » et « personne n'a répondu » sont deux réponses : la note
    # les dit, et le brouillon la conserve pour les rendus suivants. Un échec laisse
    # le bouton de relance en place, donc rien n'est marqué comme définitif.
    finalized = outcome.answered
    updated_draft = {
        **data,
        "variants": _merge_keywords(data.get("variants"), outcome.variants),
        "exclude_keywords": _merge_keywords(
            data.get("exclude_keywords"), outcome.exclusions
        ),
        "keyword_note": _keyword_note(outcome),
        "keywords_finalized": finalized,
    }
    await state.update_data(
        variants=updated_draft["variants"],
        exclude_keywords=updated_draft["exclude_keywords"],
        keyword_note=updated_draft["keyword_note"],
        keywords_finalized=finalized,
    )

    recap, keyboard = build_creation_recap(updated_draft)
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(recap, reply_markup=keyboard)


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

        if isbn:
            # 1. One-Shot ou tome spécifique : Persistance définitive de l'ISBN en base
            book = await book_repo.get_by_isbn13(isbn)
            if not book:
                created = await book_repo.create(
                    title=name,
                    normalized_title=normalize_text(name),
                    isbn13=isbn,
                    author=author,
                    publisher=publisher,
                )
                # `create` rend un objet dont `market_cache` n'est pas chargé : la
                # confirmation lit cette relation (`format_cote_lines`) après le commit,
                # et une lecture paresseuse y lèverait en async. On relit le livre comme
                # le ferait `get_by_isbn13`, pour que la confirmation n'ait qu'un chemin,
                # que le livre soit nouveau ou déjà au catalogue.
                book = await book_repo.get_by_id(created.id)

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

        # La cote part après le commit : elle lit le livre dans sa propre session, et
        # surtout elle ne doit pas faire attendre les marchands avant la confirmation.
        if book is not None:
            schedule_book_pricing(book.id, reason="alert_created")
        else:
            # Série sans ISBN : rien à coter encore. On lance la résolution par le nom
            # de l'alerte, pour que les tomes identifiés soient cotés avant l'annonce.
            _schedule_series_resolution(alert_id=alert.id, name=name, author=author)

        price_str = format_price_cents(max_price)

        # La confirmation reprend la carte de récap — même ordre, mêmes libellés — : ce
        # qui vient d'être validé se relit ligne pour ligne. Les mots-clés sont
        # échappés comme dans le récap : ils viennent du moteur IA, et un `<b>` glissé
        # dedans ferait rejeter tout le message par Telegram.
        lines = ["✅ <b>Alerte créée</b>\n"]
        if isbn:
            lines.append(f"<b>ISBN :</b> <code>{html.escape(isbn)}</code>")
        lines.append(f"<b>Titre :</b> {html.escape(name)}")
        if author:
            lines.append(f"<b>Auteur :</b> {html.escape(author)}")
        if publisher:
            lines.append(f"<b>Éditeur :</b> {html.escape(publisher)}")
        lines.append("")
        lines.append(f"<b>Prix maximum :</b> {price_str}")
        lines.append(f"<b>États acceptés :</b> {html.escape(format_conditions(conditions))}")
        lines.append(f"<b>Mots-clés inclus :</b> {_join_escaped(includes)}\n")

        # La cote déjà connue est affichée telle quelle ; sinon la confirmation
        # annonce qu'elle arrive, plutôt que de laisser croire qu'il n'y a pas de prix.
        # Une alerte de série n'a pas encore de livre tant qu'aucun tome n'a été
        # identifié : dans ce cas, il n'y a rien à coter, donc rien à annoncer.
        # Ni marchand, ni date : la même cote est servie par plusieurs plateformes,
        # et `historical_low_at` est réécrit à chaque rafraîchissement de la cote —
        # elle daterait du moment de la création, pas du prix constaté.
        cote_lines = format_cote_lines(book)
        if cote_lines or book is not None:
            lines.extend(cote_lines or [PENDING_COTE_LINE])
            lines.append("")

        lines.append(_CONFIRMATION_FOOTER)
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
        price_str = format_price_cents(max_price)
        cond_str = format_conditions(conditions)

        lines = [
            "✅ <b>Alerte créée</b>\n",
            f"{cat_emoji} <b>{html.escape(name)}</b> ({cat_label})",
            "",
            f"<b>Prix maximum :</b> {price_str}",
            f"<b>États acceptés :</b> {html.escape(cond_str)}",
            f"<b>Mots-clés inclus :</b> {_join_escaped(includes)}",
        ]
        if variants:
            lines.append(f"<b>Variantes :</b> {_join_escaped(variants)}")
        if excludes:
            lines.append(f"<b>Mots-clés exclus :</b> {_join_escaped(excludes)}")
        lines.append("")
        lines.append(_CONFIRMATION_FOOTER)
        resp_text = "\n".join(lines)

    vinted_btn = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Voir la recherche Vinted", url=vinted_url)]
        ]
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(resp_text, reply_markup=vinted_btn)
