"""Flux de gestion, modification (/edit), pause/reprise (/pause, /resume)
et suppression (/delete) d'alertes.
"""

import html
import logging
import urllib.parse
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.formatter import format_alert_card
from app.bot.handlers.alerts_helpers import keyword_note
from app.bot.handlers.alerts_view import show_alerts, view_single_alert
from app.bot.handlers.autotune import pop_pending_variants, save_pending_variants
from app.bot.keyboards.alerts import (
    get_alert_card_keyboard,
    get_alert_edit_fields_keyboard,
    get_cancel_edit_keyboard,
    get_delete_confirmation_keyboard,
    get_edit_category_selection_keyboard,
    get_edit_conditions_keyboard,
)
from app.bot.labels import (
    BTN_BACK,
    BTN_BACK_TO_ALERT,
    FIELD_EXCLUDE_KEYWORDS,
    FIELD_INCLUDE_KEYWORDS,
    FIELD_ISBN,
    FIELD_VARIANTS,
)
from app.bot.states.alert_creation import AlertEditState
from app.core.constants import STANDARD_CONDITIONS, AlertStatus, CategorySlug
from app.core.tasks import safe_create_task
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.category_repo import CategoryRepository
from app.db.session import AsyncSessionLocal
from app.domain.entities.alert_preparation import preparation_request
from app.domain.rules.keyword_hygiene import sanitize_exclusions, sanitize_variants
from app.services.ai.modules.keyword_variants import keyword_variants_module
from app.services.ai.schemas import BookContext, book_context_from
from app.services.alert_service import (
    AlertServiceError,
    parse_comma_separated_list,
    parse_price_to_cents,
    validate_and_parse_vinted_url,
)
from app.services.book_market_pricing import schedule_book_pricing
from app.services.book_metadata_service import BookMetadata
from app.use_cases.prepare_bd_alert import prepare_bd_alert
from app.utils.isbn import extract_clean_isbn

logger = logging.getLogger(__name__)

_active_variant_alerts: set[str] = set()

router = Router(name="alerts_manage")


def _get_alert_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "AlertRepository", AlertRepository)(session)


def _get_category_repo(session: Any) -> Any:
    import app.bot.handlers.alerts as alerts_mod

    return getattr(alerts_mod, "CategoryRepository", CategoryRepository)(session)


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
    alert_repo = _get_alert_repo(db_session)
    total_count = await alert_repo.count_by_user(db_user.id)
    if total_count == 0:
        msg = "Aucune alerte active pour le moment. Utilise /add pour en créer une."
        if isinstance(target, CallbackQuery):
            if target.message and hasattr(target.message, "edit_text"):
                await target.message.edit_text(msg)
        else:
            await target.answer(msg)
        return

    cat_repo = _get_category_repo(db_session)
    active_categories = await cat_repo.get_all_active()
    counts = await alert_repo.count_by_user_grouped_by_category(db_user.id)

    text = (
        "✏️ <b>Modifier l'alerte</b>\n\n"
        "Choisis une catégorie pour afficher et modifier tes alertes :"
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

    alert_repo = _get_alert_repo(db_session)
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
        preparation=getattr(alert, "preparation", None),
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
    state: FSMContext | None = None,
) -> None:
    """Affiche les options de modification pour une alerte donnée."""
    await callback.answer()
    if state:
        await state.clear()
    alert_id = uuid.UUID(callback.data.split(":")[1])  # type: ignore

    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    cat_slug = alert.category.slug if alert.category else None
    cat_emoji = alert.category.emoji if alert.category else ""
    emoji_prefix = f"{cat_emoji} " if cat_emoji else ""
    text = (
        f"✏️ <b>Modifier l'alerte</b>\n\n"
        f"{emoji_prefix}« <b>{html.escape(alert.name)}</b> »\n"
        "Choisis le critère à mettre à jour :"
    )
    keyboard = get_alert_edit_fields_keyboard(alert.id, category_slug=cat_slug)
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(text, reply_markup=keyboard)


async def _show_conditions_menu(
    target: CallbackQuery | Message,
    alert: Any,
) -> None:
    """Affiche le menu de bascule des conditions acceptées."""
    text = (
        f"✏️ <b>États acceptés</b>\n\n"
        f"Alerte : « {html.escape(alert.name)} »\n"
        "Coche ou décoche les états autorisés pour cette alerte."
    )
    keyboard = get_edit_conditions_keyboard(alert.id, alert.accepted_conditions)
    if hasattr(target, "message") and target.message and hasattr(target.message, "edit_text"):
        await target.message.edit_text(text, reply_markup=keyboard)
    elif hasattr(target, "answer"):
        await target.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("ed_f:"))
async def prompt_new_field_value(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: User,
    db_session: Any,
) -> None:
    """Affiche l'invite épurée avec bloc '1 Clic pour Copier' ou cases à cocher."""
    parts = callback.data.split(":")  # type: ignore
    alert_id = uuid.UUID(parts[1])
    field = parts[2]

    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    if field == "market":
        from app.bot.handlers.market_panel import open_panel
        from app.use_cases.view_market_details import ViewMarketDetailsUseCase

        await callback.answer()
        panel = await ViewMarketDetailsUseCase(db_session).execute_for_alert(alert.id, db_user.id)
        if panel is not None:
            await open_panel(callback, panel, user_id=db_user.id)
        return

    # 1. Conditions : cases à cocher interactives
    if field == "conditions":
        await callback.answer()
        await state.clear()
        await _show_conditions_menu(callback, alert)
        return

    # Actions intelligentes IA
    if field == "ai_variants":
        await state.clear()
        alert_key = str(alert.id)
        if alert_key in _active_variant_alerts:
            await callback.answer("⏳ Recherche déjà en cours...", show_alert=True)
            return

        _active_variant_alerts.add(alert_key)
        await callback.answer()

        loading_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⏳ Recherche en cours...",
                        callback_data="noop",
                    )
                ]
            ]
        )
        if callback.message and hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(
                "⏳ Recherche des variantes orthographiques en cours…",
                reply_markup=loading_keyboard,
            )

        try:
            from app.services.ai.modules.keyword_variants import keyword_variants_module

            variants = await keyword_variants_module.generate_variants(alert.name)
            current_variants = set(alert.variants or [])
            existing_vars = {x.lower() for x in current_variants}
            new_variants = [v for v in variants if v.lower() not in existing_vars]

            if not variants:
                empty_keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=BTN_BACK_TO_ALERT,
                                callback_data=f"al_edit:{alert.id}",
                            )
                        ]
                    ]
                )
                if callback.message and hasattr(callback.message, "edit_text"):
                    await callback.message.edit_text(
                        "Aucune variante trouvée : le moteur n'a pas identifié "
                        f"de variante pertinente pour « {html.escape(alert.name)} ».",
                        reply_markup=empty_keyboard,
                    )
                return

            if not new_variants:
                existing_keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=BTN_BACK_TO_ALERT,
                                callback_data=f"al_edit:{alert.id}",
                            )
                        ]
                    ]
                )
                var_str = ", ".join(variants)
                if callback.message and hasattr(callback.message, "edit_text"):
                    await callback.message.edit_text(
                        "Toutes les variantes suggérées sont déjà actives sur cette alerte.",
                        reply_markup=existing_keyboard,
                    )
                return

            save_pending_variants(alert.id, new_variants)
            var_str = ", ".join(new_variants)
            text = (
                f"✏️ <b>Variantes suggérées</b>\n\n"
                f"Alerte : « {html.escape(alert.name)} »\n"
                f"<code>{html.escape(var_str)}</code>\n\n"
                f"Ces termes permettront de matcher les variantes orthographiques. "
                f"Ajouter ces {len(new_variants)} variante(s) à l'alerte ?"
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Ajouter aux variantes",
                            callback_data=f"ai_var_apply:{alert.id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=BTN_BACK,
                            callback_data=f"al_edit:{alert.id}",
                        )
                    ],
                ]
            )
            if callback.message and hasattr(callback.message, "edit_text"):
                await callback.message.edit_text(text, reply_markup=keyboard)
        except Exception as err:
            logger.error("Erreur lors de la suggestion de variantes pour %s: %s", alert.name, err)
            err_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=BTN_BACK_TO_ALERT,
                            callback_data=f"al_edit:{alert.id}",
                        )
                    ]
                ]
            )
            if callback.message and hasattr(callback.message, "edit_text"):
                err_msg = (
                    f"⚠️ Erreur lors de la recherche des variantes pour "
                    f"« {html.escape(alert.name)} ».\nRéessaie dans un instant."
                )
                await callback.message.edit_text(
                    err_msg,
                    reply_markup=err_keyboard,
                )
        finally:
            _active_variant_alerts.discard(alert_key)
        return

    if field == "ai_autotune":
        await state.clear()
        from app.bot.handlers.autotune import process_autotune_for_alert

        await process_autotune_for_alert(callback, alert, db_session)
        return

    # 2. Tous les autres champs : bloc <code> cliquable pour copier en 1 clic
    await callback.answer()
    await state.set_state(AlertEditState.waiting_for_new_value)
    await state.update_data(edit_alert_id=str(alert.id), edit_field=field)

    labels = {
        "name": ("Nom / Titre", "Colle dans la zone de texte, modifie et envoie :"),
        "isbn": (FIELD_ISBN, "Colle dans la zone de texte, modifie et envoie :"),
        "url": ("Lien Vinted", "Colle dans la zone de texte, modifie et envoie :"),
        "include_keywords": (
            FIELD_INCLUDE_KEYWORDS,
            "Colle dans la zone de texte, modifie et envoie :",
        ),
        "exclude_keywords": (
            FIELD_EXCLUDE_KEYWORDS,
            "Colle dans la zone de texte, modifie et envoie (ou /skip pour tout effacer) :",
        ),
        "variants": (
            FIELD_VARIANTS,
            "Colle dans la zone de texte, modifie et envoie (ou /skip pour tout effacer) :",
        ),
        "max_price": (
            "Prix maximum (en euros)",
            "Colle dans la zone de texte, modifie et envoie :",
        ),
    }
    title, prompt_hint = labels.get(field, (field, "Envoie la nouvelle valeur :"))

    if field in ("exclude_keywords", "include_keywords", "variants"):
        items = getattr(alert, field, []) or []
        current_str = ", ".join(items)
    elif field == "name":
        current_str = alert.name
    elif field == "isbn":
        current_str = alert.isbn or ""
    elif field == "url":
        current_str = alert.vinted_search_url or ""
    elif field == "max_price":
        current_str = (
            f"{alert.max_item_price_cents / 100:.2f}".rstrip("0").rstrip(".")
            if alert.max_item_price_cents
            else ""
        )
    else:
        current_str = ""

    if current_str:
        text = (
            f"✏️ <b>{title}</b>\n\n"
            f"Valeur actuelle :\n<code>{html.escape(current_str)}</code>\n\n"
            f"{prompt_hint}"
        )
    else:
        text = (
            f"✏️ <b>{title}</b>\n\n"
            f"Valeur actuelle : Non renseignée\n\n"
            f"{prompt_hint}"
        )

    keyboard = get_cancel_edit_keyboard(alert.id)
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("ed_c_tog:"))
async def handle_edit_condition_toggle(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Bascule l'état d'une condition pour l'alerte."""
    parts = callback.data.split(":")  # type: ignore
    alert_id = uuid.UUID(parts[1])
    idx = int(parts[2])

    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    target_cond = STANDARD_CONDITIONS[idx]
    if alert.accepted_conditions is None or len(alert.accepted_conditions) == 0:
        current_conds = list(STANDARD_CONDITIONS)
    else:
        current_conds = list(alert.accepted_conditions)

    if target_cond in current_conds:
        current_conds.remove(target_cond)
    else:
        current_conds.append(target_cond)

    if len(current_conds) == len(STANDARD_CONDITIONS) or len(current_conds) == 0:
        alert.accepted_conditions = None
    else:
        alert.accepted_conditions = current_conds

    await db_session.commit()
    await callback.answer()
    await _show_conditions_menu(callback, alert)


@router.callback_query(F.data.startswith("ed_c_all:"))
async def handle_edit_condition_all(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Active tous les états pour l'alerte."""
    parts = callback.data.split(":")  # type: ignore
    alert_id = uuid.UUID(parts[1])

    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    alert.accepted_conditions = None
    await db_session.commit()
    await callback.answer("🌐 Tous les états acceptés.")
    await _show_conditions_menu(callback, alert)


async def _lookup_isbn_notice(isbn13: str) -> BookMetadata | None:
    """Notice que les autorités rendent pour cet ISBN, None si personne n'en a une.

    L'ISBN inconnu et la panne réseau se lisent pareil : aucun des deux ne prouve une
    contradiction, et la saisie de l'utilisateur ne doit jamais être bloquée par le
    second. La notice sert deux fois — au filet anti-mauvais ISBN, et à la fiche sur
    laquelle le moteur raisonne — donc elle est demandée **une** fois.
    """
    from app.services.book_metadata_service import BookMetadataService

    try:
        return await BookMetadataService().lookup_by_isbn(isbn13)
    except Exception as exc:
        logger.warning("Vérification BNF impossible pour l'ISBN %s : %s", isbn13, exc)
        return None


def _conflicting_isbn_message(
    notice: BookMetadata | None,
    isbn13: str,
    alert_name: str,
) -> str | None:
    """Message d'erreur si la notice rattache cet ISBN à un autre ouvrage que l'alerte."""
    from app.services.book_metadata_service import TITLE_SIMILARITY_MIN, title_similarity

    if notice is None or title_similarity(notice.title, alert_name) >= TITLE_SIMILARITY_MIN:
        return None

    real_author = f" de {notice.author}" if notice.author else ""
    return (
        f"Cet ISBN ({isbn13}) correspond à « {notice.title} »{real_author}, "
        f"pas à « {alert_name} »."
    )


async def _refresh_alert_keywords(
    alert_id: uuid.UUID,
    *,
    expected_isbn: str,
    name: str,
    category_slug: str,
    book_context: BookContext | None,
    target: Message,
) -> None:
    """Rend à une alerte les mots-clés de l'ouvrage qu'elle surveille désormais.

    Appelée en arrière-plan après un changement d'ISBN. Les variantes et exclusions de
    l'ouvrage précédent ont été retirées **avant** le commit : sans cette recherche,
    l'alerte resterait muette jusqu'à ce que l'utilisateur pense à rappuyer sur le
    bouton. Le verdict est annoncé tel quel — un moteur muet ne peut pas se lire comme
    un avis rendu (`keyword_note`).

    L'écriture est gardée par l'ISBN qui l'a déclenchée : si l'alerte en porte un autre,
    la réponse décrit un ouvrage déjà remplacé et repart sans être écrite.
    """
    outcome = await keyword_variants_module.generate_variants_and_exclusions(
        name, category_slug=category_slug, context=book_context
    )

    async with AsyncSessionLocal() as session:
        alert = await _get_alert_repo(session).get_by_id(alert_id)
        if alert is None or alert.isbn != expected_isbn:
            logger.info(
                "Mots-clés de « %s » non écrits : l'alerte ne porte plus l'ISBN %s.",
                name,
                expected_isbn,
            )
            return
        alert.variants = sanitize_variants(outcome.variants, name)
        alert.exclude_keywords = sanitize_exclusions(outcome.exclusions, name)
        await session.commit()

    note = keyword_note(outcome)
    if note:
        await target.answer(f"<i>{note}</i>", parse_mode="HTML")


@router.message(AlertEditState.waiting_for_new_value)
async def save_edited_field_value(
    message: Message,
    state: FSMContext,
    db_user: User,
    db_session: Any,
) -> None:
    """Applique la modification du champ en base de données et affiche l'alerte mise à jour."""
    data = await state.get_data()
    alert_id = uuid.UUID(data["edit_alert_id"])
    field = data["edit_field"]
    raw_text = (message.text or "").strip()

    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await message.answer("Alerte introuvable.")
        await state.clear()
        return

    try:
        # L'autorité est consultée avant le verrou SQL. La cohérence du titre
        # sera ensuite jugée sur l'alerte relue, et non sur l'objet initial.
        notice = None
        clean_isbn = None
        if field == "isbn" and raw_text.lower() not in (
            "/skip", "-", "aucun", "aucune", "clear", "supprimer"
        ):
            clean_isbn = extract_clean_isbn(raw_text)
            if not clean_isbn:
                raise AlertServiceError(
                    "ISBN invalide : 10 ou 13 chiffres avec une clé de contrôle correcte."
                )
            notice = await _lookup_isbn_notice(clean_isbn)
        if not await alert_repo.lock_for_edit(alert):
            await db_session.rollback()
            await message.answer("Alerte introuvable.")
            await state.clear()
            return
        original_name, original_isbn = alert.name, alert.isbn
        book_to_price: uuid.UUID | None = None
        # L'ISBN retenu quand il remplace celui de l'alerte : c'est le seul cas où les
        # mots-clés de l'ouvrage précédent doivent partir, et donc le seul où la
        # recherche du moteur est relancée.
        replaced_isbn: str | None = None
        new_work_context: BookContext | None = None
        if field in ("exclude_keywords", "include_keywords", "variants"):
            if raw_text.lower() == "/skip" or raw_text in ("-", "aucun", "aucune"):
                parsed = []
            else:
                parsed = parse_comma_separated_list(raw_text)

            if field == "include_keywords" and not parsed:
                raise AlertServiceError("Au moins un mot-clé requis pour surveiller l'alerte.")
            # Les variantes et exclusions saisies peuvent venir d'un copier-coller du
            # moteur : le nettoyeur est le dernier filtre avant la base. Les mots-clés
            # *inclus*, eux, sont l'intention de l'utilisateur et restent tels quels.
            if field == "variants":
                parsed = sanitize_variants(parsed, alert.name)
            elif field == "exclude_keywords":
                parsed = sanitize_exclusions(parsed, alert.name)

            setattr(alert, field, parsed)

        elif field == "name":
            if len(raw_text) < 2:
                raise AlertServiceError("Le nom doit contenir au moins 2 caractères.")
            alert.name = raw_text
            if alert.category and alert.category.slug in (
                CategorySlug.TECH.value,
                CategorySlug.CLOTHES.value,
                CategorySlug.GAMING.value,
            ):
                alert.vinted_search_url = (
                    f"https://www.vinted.fr/catalog?search_text="
                    f"{urllib.parse.quote_plus(raw_text)}&order=newest_first"
                )
        elif field == "isbn":
            if raw_text.lower() in ("/skip", "-", "aucun", "aucune", "clear", "supprimer"):
                alert.isbn = None
                alert.vinted_search_url = (
                    f"https://www.vinted.fr/catalog?search_text="
                    f"{urllib.parse.quote_plus(alert.name)}&order=newest_first"
                )
            else:
                assert clean_isbn is not None
                # Filet anti-mauvais ISBN : la notice doit rattacher ce code à CETTE
                # œuvre. Une panne réseau ne bloque pas la saisie (aucune preuve de
                # conflit).
                conflicting = _conflicting_isbn_message(notice, clean_isbn, alert.name)
                if conflicting:
                    raise AlertServiceError(conflicting)
                # Changer d'ISBN, c'est changer d'ouvrage : les variantes et les
                # exclusions ont été raisonnées sur la fiche précédente, et une
                # variante suffit à contourner la contrainte d'inclusion. Elles
                # partent dans le même commit que l'ISBN, et la recherche repart
                # après — l'alerte ne peut pas les garder pour un livre qu'elle ne
                # surveille plus.
                if alert.isbn != clean_isbn:
                    replaced_isbn = clean_isbn
                    new_work_context = book_context_from(notice) if notice else None
                    alert.variants = []
                    alert.exclude_keywords = []
                    # Une proposition de variantes encore en attente de confirmation
                    # décrit l'ouvrage précédent : l'appliquer après ce changement
                    # ferait matcher l'alerte sur l'ancien livre. Le bouton de la
                    # proposition le dit alors (« Les suggestions ont expiré. »).
                    pop_pending_variants(alert.id)
                alert.isbn = clean_isbn
                # Nettoyer d'éventuels ISBN numériques résiduels dans les mots-clés
                alert.include_keywords = [
                    kw for kw in (alert.include_keywords or [])
                    if not (kw.isdigit() and len(kw) >= 10)
                ]
                # Mettre à jour l'URL de recherche Vinted
                base_vinted = "https://www.vinted.fr/catalog?search_start_type=item_page_isbn"
                alert.vinted_search_url = (
                    f"{base_vinted}&search_text={clean_isbn}&order=newest_first"
                )

                # Propagation globale en base : livre et liaison alerte-livre. La cote
                # est relancée après le commit, pour que le livre existe déjà quand
                # la tâche de fond ouvrira sa session.
                from app.core.constants import MatchConfidence
                from app.db.repositories.book_repo import BookRepository
                from app.utils.text import normalize_text

                book_repo = BookRepository(db_session)

                book = await book_repo.get_by_isbn13(clean_isbn)
                if not book:
                    book = await book_repo.create(
                        title=alert.name,
                        normalized_title=normalize_text(alert.name),
                        isbn13=clean_isbn,
                    )

                if book:
                    await book_repo.clear_alert_books(alert.id)
                    await book_repo.link_alert_to_book(alert.id, book.id, MatchConfidence.HIGH)
                    book_to_price = book.id
        elif field == "url":
            parsed_url = validate_and_parse_vinted_url(raw_text)
            alert.vinted_search_url = parsed_url["url"]
        elif field == "max_price":
            alert.max_item_price_cents = parse_price_to_cents(raw_text)
        elif field == "conditions":
            alert.accepted_conditions = (
                parse_comma_separated_list(raw_text) if raw_text else None
            )

        preparation_changed = alert.isbn != original_isbn or (
            field == "name" and alert.name != original_name and not alert.isbn
        )
        if preparation_changed:
            await alert_repo.invalidate_preparation(alert, author_hint=None)
        await db_session.commit()
        await state.clear()

        if preparation_changed and preparation_request(alert) is not None:
            await prepare_bd_alert(alert.id)
            refreshed = await alert_repo.get_by_id(alert.id, refresh=True)
            if refreshed is None:
                return
            alert = refreshed
        elif book_to_price is not None:
            schedule_book_pricing(book_to_price, reason="isbn_edited")

        cat_slug = alert.category.slug if alert.category else "all"
        total_count = await alert_repo.count_by_user(db_user.id, category_slug=cat_slug)

        await message.answer("✅ Modification enregistrée.")
        card_text = format_alert_card(alert, current_index=0, total_count=total_count)
        card_message = await message.answer(
            card_text,
            reply_markup=get_alert_card_keyboard(
                alert.id,
                alert.status,
                current_page=0,
                total_pages=total_count,
                category_slug=cat_slug,
                preparation=getattr(alert, "preparation", None),
            ),
        )

        if replaced_isbn is not None:
            # Après le commit, comme la cote : la tâche ouvrira sa propre session et
            # doit trouver l'alerte déjà sur son nouvel ISBN.
            safe_create_task(
                _refresh_alert_keywords(
                    alert.id,
                    expected_isbn=replaced_isbn,
                    name=alert.name,
                    category_slug=cat_slug,
                    book_context=new_work_context,
                    target=card_message,
                ),
                name=f"alert-keywords-{alert.id}",
            )
    except AlertServiceError as exc:
        await db_session.rollback()
        await message.answer(f"⚠️ {exc}\nRéessaie (ou /cancel pour annuler) :")


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
    alert_repo = _get_alert_repo(db_session)
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
    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    await alert_repo.set_status(alert, AlertStatus.ACTIVE)
    await db_session.commit()
    if preparation_request(alert) is not None:
        try:
            await callback.answer("📊 Préparation relancée ; consulte l'alerte pour son état.")
        except Exception:
            logger.warning("Acquittement de reprise indisponible", exc_info=True)
        await prepare_bd_alert(alert.id)
        await view_single_alert(callback, db_user, db_session, answer_callback=False)
        return
    await callback.answer("▶️ Alerte réactivée.")
    await view_single_alert(callback, db_user, db_session)


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

    alert_repo = _get_alert_repo(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    msg = f"Supprimer définitivement l'alerte « <b>{html.escape(alert.name)}</b> » ?"
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

    alert_repo = _get_alert_repo(db_session)
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
            f"L'alerte « <b>{html.escape(alert_name)}</b> » et toutes ses données associées "
            "ont été supprimées définitivement.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Retour aux catégories", callback_data="ed_cat_menu"
                        )
                    ]
                ]
            ),
        )
