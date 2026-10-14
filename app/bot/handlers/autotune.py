"""Handler pour la commande /autotune et l'Auto-Tuning proactif des alertes (Use Case G)."""

import contextlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.labels import (
    BTN_ANALYSIS_RUNNING,
    BTN_APPLY_EXCLUSIONS,
    BTN_BACK_TO_ALERT,
    BTN_BACK_TO_AUTOTUNE,
    BTN_CANCEL,
    BTN_CLOSE,
    BTN_DISCARD_CHANGES,
    BTN_EDIT_ALERT_SETTINGS,
    BTN_EDIT_EXCLUSIONS,
    BTN_EDIT_EXCLUSIONS_AGAIN,
    FIELD_EXCLUDE_KEYWORDS,
    FIELD_FALSE_POSITIVES,
    FIELD_NAME,
    FIELD_SUGGESTED_EXCLUDES,
    FIELD_SUGGESTED_EXCLUDES_CUSTOM,
    FIELD_VARIANTS,
    NOT_PROVIDED,
    TITLE_AUTOTUNE,
    TITLE_EDIT_EXCLUDES,
    escape_html,
)
from app.bot.states.alert_creation import AutoTuneState
from app.db.models.alert import Alert
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository, BibliographicSnapshot
from app.db.repositories.autotune_repo import (
    clear_dislikes_for_alert,
    get_alerts_with_dislikes,
    get_disliked_titles_for_alert,
)
from app.domain.rules.keyword_hygiene import sanitize_exclusions, sanitize_variants
from app.services.ai.modules.auto_tuning import (
    auto_tuning_module,
    build_exclusion_proposal,
    get_pending_tuning,
    pop_pending_tuning,
    save_pending_tuning,
)

logger = logging.getLogger(__name__)

router = Router(name="autotune_router")

# Suggestions de variantes orthographiques en attente de confirmation. Volontairement
# en mémoire seule, à la différence du store d'exclusions : ces suggestions sont
# jetables et se régénèrent d'un simple appel à /autotune.
@dataclass(frozen=True)
class _PendingVariants:
    values: list[str]
    snapshot: BibliographicSnapshot | None


_pending_variants: dict[str, _PendingVariants] = {}
_active_tuning_alerts: set[str] = set()


def save_pending_variants(
    alert_id: uuid.UUID, variants: list[str], *, snapshot: BibliographicSnapshot | None = None,
) -> None:
    """Mémorise les variantes proposées pour cette alerte."""
    _pending_variants[str(alert_id)] = _PendingVariants(variants, snapshot)


def get_pending_variants(alert_id: uuid.UUID) -> list[str]:
    """Variantes proposées pour cette alerte, encore en attente de confirmation."""
    pending = _pending_variants.get(str(alert_id))
    return pending.values if pending else []


def pop_pending_variants(alert_id: uuid.UUID) -> list[str]:
    """Retire et renvoie les variantes proposées (confirmation ou abandon)."""
    pending = _pending_variants.pop(str(alert_id), None)
    return pending.values if pending else []


def _autotune_back_keyboard(alert_id: object) -> InlineKeyboardMarkup:
    """Clavier de sortie d'un écran d'auto-tuning : retour à la liste, ou édition."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_BACK_TO_AUTOTUNE, callback_data="ai_tune_hub")],
            [
                InlineKeyboardButton(
                    text=BTN_EDIT_ALERT_SETTINGS, callback_data=f"al_edit:{alert_id}"
                )
            ],
        ]
    )


def _proposal_keyboard(
    alert_id: object, edit_label: str = BTN_EDIT_EXCLUSIONS
) -> InlineKeyboardMarkup:
    """Clavier d'une proposition d'exclusions : appliquer, retoucher, ou abandonner."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=BTN_APPLY_EXCLUSIONS, callback_data=f"ai_tune_apply:{alert_id}"
                )
            ],
            [InlineKeyboardButton(text=edit_label, callback_data=f"ai_tune_edit:{alert_id}")],
            [
                InlineKeyboardButton(
                    text=BTN_CANCEL, callback_data=f"ai_tune_cancel:{alert_id}"
                )
            ],
        ]
    )


def _autotune_header(alert_name: str) -> str:
    """En-tête commun des écrans d'auto-tuning : titre de charte puis alerte visée."""
    return f"{TITLE_AUTOTUNE}\n\n{FIELD_NAME} : « {escape_html(alert_name)} »"


async def show_autotune_hub(
    target: Message | CallbackQuery,
    user_id: uuid.UUID,
    db_session: Any,
) -> None:
    """Affiche ou met à jour la liste des alertes avec faux positifs à traiter."""
    alerts_with_dislikes = await get_alerts_with_dislikes(user_id, db_session)

    if not alerts_with_dislikes:
        empty_text = (
            f"{TITLE_AUTOTUNE}\n\n"
            "Aucun faux positif signalé pour le moment.\n\n"
            "Comment ça marche :\n"
            "Quand une alerte t'envoie une annonce hors sujet, touche le bouton "
            "« Faux positif » sous le message.\n"
            "Relance ensuite <code>/autotune</code> : le moteur analyse les annonces "
            "signalées et suggère des exclusions adaptées."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=BTN_CLOSE, callback_data="ai_tune_close")]
            ]
        )
        if isinstance(target, CallbackQuery):
            if target.message and hasattr(target.message, "edit_text"):
                await target.message.edit_text(empty_text, reply_markup=keyboard)
        else:
            await target.answer(empty_text, reply_markup=keyboard)
        return

    buttons = [
        [
            InlineKeyboardButton(
                text=f"{alert.name} ({count} faux positif{'s' if count > 1 else ''})",
                callback_data=f"ai_tune_select:{alert.id}",
            )
        ]
        for alert, count in alerts_with_dislikes
    ]
    buttons.append([InlineKeyboardButton(text=BTN_CANCEL, callback_data="ai_tune_close")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    hub_text = (
        f"{TITLE_AUTOTUNE}\n\n"
        "Sélectionne l'alerte à analyser pour nettoyer ses faux positifs :"
    )
    if isinstance(target, CallbackQuery):
        if target.message and hasattr(target.message, "edit_text"):
            await target.message.edit_text(hub_text, reply_markup=keyboard)
    else:
        await target.answer(hub_text, reply_markup=keyboard)


@router.message(Command("autotune", "tune"))
async def handle_autotune_command(
    message: Message,
    db_user: User,
    db_session: Any,
) -> None:
    """Analyse les faux positifs signalés et propose des exclusions intelligentes."""
    if not auto_tuning_module.is_enabled:
        await message.answer(
            "L'auto-tuning est désactivé dans la configuration de ce bot."
        )
        return

    # Vérifier l'argument optionnel (nom d'alerte ou recherche)
    args = (message.text or "").strip().split(maxsplit=1)
    target_query = args[1].strip().lower() if len(args) > 1 else None

    # Si une alerte spécifique est ciblée
    if target_query:
        alert_repo = AlertRepository(db_session)
        matched_alerts = await alert_repo.search_by_user(db_user.id, target_query)
        if not matched_alerts:
            await message.answer(
                f"Aucune alerte ne correspond à « {escape_html(target_query)} »."
            )
            return
        target_alert = matched_alerts[0]
        await process_autotune_for_alert(message, target_alert, db_session)
        return

    # Recherche globale : trouver toutes les alertes de l'utilisateur avec des faux positifs
    alerts_with_dislikes = await get_alerts_with_dislikes(db_user.id, db_session)

    # Si une seule alerte a des faux positifs, la traiter directement
    if len(alerts_with_dislikes) == 1:
        target_alert, _ = alerts_with_dislikes[0]
        await process_autotune_for_alert(message, target_alert, db_session)
        return

    # Sinon, afficher le hub complet
    await show_autotune_hub(message, db_user.id, db_session)


def parse_exclusions_input(text: str) -> list[str]:
    """Découpe et normalise une liste de mots-clés d'exclusion utilisateur.

    Accepte les séparateurs par virgule, point-virgule et retours à la ligne,
    tout en nettoyant les puces ou tirets markdown éventuels.
    """
    normalized = text.replace("\n", ",").replace(";", ",")
    items: list[str] = []
    seen: set[str] = set()
    for raw in normalized.split(","):
        cleaned = raw.strip().lstrip("-•* \t").strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            items.append(cleaned)
    return items


async def process_autotune_for_alert(
    target: Message | CallbackQuery,
    alert: Alert,
    db_session: Any,
) -> None:
    """Exécute l'analyse IA des faux positifs pour une alerte donnée avec transparence totale."""
    alert_key = str(alert.id)
    if alert_key in _active_tuning_alerts:
        if isinstance(target, CallbackQuery):
            await target.answer("Une analyse est déjà en cours pour cette alerte.", show_alert=True)
        return

    _active_tuning_alerts.add(alert_key)
    loading_text = f"{_autotune_header(alert.name)}\n\nAnalyse des faux positifs en cours…"
    loading_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_ANALYSIS_RUNNING, callback_data="noop")]
        ]
    )

    if isinstance(target, CallbackQuery):
        await target.answer()
        if target.message and hasattr(target.message, "edit_text"):
            await target.message.edit_text(loading_text, reply_markup=loading_kb)
    else:
        await target.answer(loading_text, reply_markup=loading_kb)

    back_kb = _autotune_back_keyboard(alert.id)

    try:
        disliked_titles = await get_disliked_titles_for_alert(alert.id, db_session)
        if not disliked_titles:
            msg = (
                f"{_autotune_header(alert.name)}\n\n"
                "Aucun faux positif enregistré pour cette alerte.\n\n"
                "Pour que l'auto-tuning apprenne de tes préférences :\n"
                "• Signale les annonces indésirables avec le bouton « Faux positif ».\n"
                "• Le moteur proposera ensuite des exclusions adaptées."
            )
            if isinstance(target, CallbackQuery):
                if target.message and hasattr(target.message, "edit_text"):
                    await target.message.edit_text(msg, reply_markup=back_kb)
            else:
                await target.answer(msg, reply_markup=back_kb)
            return

        proposal = await build_exclusion_proposal(
            alert_name=alert.name,
            already_excluded=list(alert.exclude_keywords or []),
            disliked_titles=disliked_titles,
            min_dislikes=1,
        )

        titles_bullet = "\n".join(f"• <i>{escape_html(t)}</i>" for t in disliked_titles[:5])

        if proposal is None:
            # Purger les faux positifs résolus déjà couverts par les exclusions existantes
            await clear_dislikes_for_alert(alert.id, db_session)
            await db_session.commit()

            active_excl_str = (
                escape_html(", ".join(alert.exclude_keywords))
                if alert.exclude_keywords
                else NOT_PROVIDED
            )
            res_text = (
                f"{_autotune_header(alert.name)}\n\n"
                f"{FIELD_FALSE_POSITIVES} ({len(disliked_titles)}) :\n"
                f"{titles_bullet}\n\n"
                f"{FIELD_EXCLUDE_KEYWORDS} : <code>{active_excl_str}</code>\n\n"
                "Toutes les exclusions pertinentes sont déjà actives sur l'alerte.\n"
                "Les faux positifs traités ont été retirés de la liste."
            )
            if isinstance(target, CallbackQuery):
                if target.message and hasattr(target.message, "edit_text"):
                    await target.message.edit_text(res_text, reply_markup=back_kb)
            else:
                await target.answer(res_text, reply_markup=back_kb)
            return

        # Mettre en cache pour la confirmation
        await save_pending_tuning(alert.id, proposal.exclusions)

        excl_str = ", ".join(proposal.exclusions)
        explanation = proposal.explanation or ""
        exp_text = f"\n<i>{escape_html(explanation)}</i>\n" if explanation else ""

        text = (
            f"{_autotune_header(alert.name)}\n\n"
            f"{FIELD_FALSE_POSITIVES} ({len(disliked_titles)}) :\n"
            f"{titles_bullet}\n"
            f"{exp_text}\n"
            f"{FIELD_SUGGESTED_EXCLUDES} :\n"
            f"<code>{escape_html(excl_str)}</code>\n\n"
            "Appliquer ces exclusions à l'alerte ?"
        )
        keyboard = _proposal_keyboard(alert.id)

        if isinstance(target, CallbackQuery):
            if target.message and hasattr(target.message, "edit_text"):
                await target.message.edit_text(text, reply_markup=keyboard)
        else:
            await target.answer(text, reply_markup=keyboard)

    except Exception as err:
        logger.error("Erreur lors de l'Auto-Tuning pour %s: %s", alert.name, err)
        err_text = (
            f"{_autotune_header(alert.name)}\n\n"
            "L'analyse n'a pas abouti. Réessaie dans un instant."
        )
        if isinstance(target, CallbackQuery):
            if target.message and hasattr(target.message, "edit_text"):
                await target.message.edit_text(err_text, reply_markup=back_kb)
        else:
            await target.answer(err_text, reply_markup=back_kb)
    finally:
        _active_tuning_alerts.discard(alert_key)


@router.callback_query(F.data.startswith("ai_tune_select:"))
async def handle_tune_select(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Traite la sélection d'une alerte à auto-tuner."""
    parts = (callback.data or "").split(":", 1)
    if len(parts) < 2:
        return
    try:
        alert_id = uuid.UUID(parts[1])
    except ValueError:
        return

    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    await process_autotune_for_alert(callback, alert, db_session)


@router.callback_query(F.data.startswith("ai_tune_apply:"))
async def handle_tune_apply(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Applique les exclusions suggérées par l'auto-tuning à l'alerte."""
    parts = (callback.data or "").split(":", 1)
    if len(parts) < 2:
        return
    try:
        alert_id = uuid.UUID(parts[1])
    except ValueError:
        return

    # L'alerte est validée AVANT de consommer la suggestion : une erreur de lecture
    # ne doit pas détruire une analyse payante, l'utilisateur peut la réappliquer.
    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    exclusions = await pop_pending_tuning(alert_id)
    if not exclusions:
        await callback.answer("Les suggestions ont expiré. Relance /autotune.", show_alert=True)
        return

    current_excludes = list(alert.exclude_keywords or [])
    for exc in exclusions:
        if exc.lower() not in [e.lower() for e in current_excludes]:
            current_excludes.append(exc)

    # Les exclusions viennent du moteur : le nettoyeur est le dernier filtre avant
    # la base. Une exclusion numérique nue rejetterait toute annonce chiffrée.
    alert.exclude_keywords = sanitize_exclusions(current_excludes, alert.name)

    # Suppression des faux positifs résolus pour cette alerte
    cleaned_count = await clear_dislikes_for_alert(alert.id, db_session)
    await db_session.commit()

    await callback.answer("Exclusions appliquées.", show_alert=True)
    back_kb = _autotune_back_keyboard(alert.id)
    if callback.message and hasattr(callback.message, "edit_text"):
        cleanup_note = (
            f"{cleaned_count} faux positif{'s' if cleaned_count > 1 else ''} "
            f"traité{'s' if cleaned_count > 1 else ''} "
            f"retiré{'s' if cleaned_count > 1 else ''} de la liste."
            if cleaned_count > 0
            else "Les faux positifs traités ont été retirés de la liste."
        )
        excludes_str = escape_html(", ".join(alert.exclude_keywords or []))
        await callback.message.edit_text(
            f"✅ <b>Exclusions appliquées</b>\n\n"
            f"{FIELD_NAME} : « {escape_html(alert.name)} »\n"
            f"{FIELD_EXCLUDE_KEYWORDS} : <code>{excludes_str}</code>\n\n"
            f"{cleanup_note}",
            reply_markup=back_kb,
        )


@router.callback_query(F.data.startswith("ai_tune_edit:"))
async def handle_tune_edit(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
    state: FSMContext,
) -> None:
    """Active la personnalisation manuelle des exclusions suggérées."""
    parts = (callback.data or "").split(":", 1)
    if len(parts) < 2:
        return
    try:
        alert_id = uuid.UUID(parts[1])
    except ValueError:
        return

    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    current_suggestions = await get_pending_tuning(alert.id)
    if not current_suggestions:
        await callback.answer("Les suggestions ont expiré. Relance /autotune.", show_alert=True)
        return

    await state.set_state(AutoTuneState.waiting_for_custom_exclusions)
    await state.update_data(tuning_alert_id=str(alert.id))

    copy_text = ", ".join(current_suggestions)
    prompt_text = (
        f"{TITLE_EDIT_EXCLUDES}\n\n"
        f"{FIELD_NAME} : « {escape_html(alert.name)} »\n"
        f"{FIELD_SUGGESTED_EXCLUDES} :\n<code>{escape_html(copy_text)}</code>\n\n"
        "Modifie le texte ci-dessus (ajoute, retire ou ajuste des termes "
        "séparés par des virgules ou retours à la ligne) puis envoie-le dans ce chat.\n\n"
        "Tape /cancel pour annuler la modification."
    )

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=BTN_DISCARD_CHANGES,
                    callback_data=f"ai_tune_back_sug:{alert.id}",
                )
            ]
        ]
    )

    await callback.answer()
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(prompt_text, reply_markup=back_kb)
    elif callback.message and hasattr(callback.message, "answer"):
        await callback.message.answer(prompt_text, reply_markup=back_kb)


@router.callback_query(F.data.startswith("ai_tune_back_sug:"))
async def handle_tune_back_to_suggestion(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
    state: FSMContext,
) -> None:
    """Restaure l'affichage des suggestions sans modifier l'état."""
    await state.clear()
    parts = (callback.data or "").split(":", 1)
    if len(parts) < 2:
        return
    try:
        alert_id = uuid.UUID(parts[1])
    except ValueError:
        return

    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    suggestions = await get_pending_tuning(alert.id)
    if not suggestions:
        await callback.answer("Les suggestions ont expiré.", show_alert=True)
        await show_autotune_hub(callback, db_user.id, db_session)
        return

    excl_str = ", ".join(suggestions)
    text = (
        f"{_autotune_header(alert.name)}\n\n"
        f"{FIELD_SUGGESTED_EXCLUDES} :\n"
        f"<code>{escape_html(excl_str)}</code>\n\n"
        "Appliquer ces exclusions à l'alerte ?"
    )
    keyboard = _proposal_keyboard(alert.id)
    await callback.answer()
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(text, reply_markup=keyboard)


@router.message(AutoTuneState.waiting_for_custom_exclusions)
async def handle_custom_exclusions_input(
    message: Message,
    db_user: User,
    db_session: Any,
    state: FSMContext,
) -> None:
    """Reçoit la liste modifiée d'exclusions par l'utilisateur et met à jour l'aperçu."""
    raw_text = (message.text or "").strip()
    if raw_text.lower() in ("/cancel", "annuler"):
        data = await state.get_data()
        alert_id_str = data.get("tuning_alert_id")
        await state.clear()
        if alert_id_str:
            with contextlib.suppress(ValueError):
                await pop_pending_tuning(uuid.UUID(alert_id_str))
        await message.answer("Modification annulée. Tape /autotune pour recommencer.")
        return

    data = await state.get_data()
    alert_id_str = data.get("tuning_alert_id")
    if not alert_id_str:
        await state.clear()
        await message.answer("Session expirée. Veuillez relancer /autotune.")
        return

    parsed = parse_exclusions_input(raw_text)
    if not parsed:
        await message.answer(
            f"{TITLE_EDIT_EXCLUDES}\n\n"
            f"{FIELD_SUGGESTED_EXCLUDES} : {NOT_PROVIDED}\n\n"
            "Envoie au moins un mot-clé, séparé par une virgule ou "
            "un retour à la ligne, ou tape /cancel pour annuler."
        )
        return

    await save_pending_tuning(uuid.UUID(alert_id_str), parsed)
    await state.clear()

    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(uuid.UUID(alert_id_str), user_id=db_user.id)
    alert_name = alert.name if alert else "Alerte"

    excl_str = ", ".join(parsed)
    text = (
        f"{_autotune_header(alert_name)}\n\n"
        f"{FIELD_SUGGESTED_EXCLUDES_CUSTOM} :\n"
        f"<code>{escape_html(excl_str)}</code>\n\n"
        "Appliquer cette liste d'exclusions à l'alerte ?"
    )
    keyboard = _proposal_keyboard(alert_id_str, BTN_EDIT_EXCLUSIONS_AGAIN)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("ai_tune_cancel"))
async def handle_tune_cancel(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
    state: FSMContext | None = None,
) -> None:
    """Annule l'opération d'auto-tuning (conserve les faux positifs) et permet de revenir au hub."""
    if state is not None:
        await state.clear()
    parts = (callback.data or "").split(":", 1)
    alert_id = parts[1] if len(parts) > 1 else None
    if alert_id:
        with contextlib.suppress(ValueError):
            await pop_pending_tuning(uuid.UUID(alert_id))
    await callback.answer("Opération annulée.")

    if not alert_id:
        await show_autotune_hub(callback, db_user.id, db_session)
        return

    back_kb = _autotune_back_keyboard(alert_id)
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(
            "Auto-tuning annulé.\n\n"
            "Les faux positifs sont conservés pour une analyse ultérieure.",
            reply_markup=back_kb,
        )


@router.callback_query(F.data.startswith("ai_tune_dismiss:"))
async def handle_tune_dismiss(
    callback: CallbackQuery,
) -> None:
    """Ignore la suggestion proactive d'auto-tuning."""
    parts = (callback.data or "").split(":", 1)
    if len(parts) > 1 and parts[1]:
        with contextlib.suppress(ValueError):
            await pop_pending_tuning(uuid.UUID(parts[1]))
    await callback.answer("Suggestion ignorée.")
    if callback.message and hasattr(callback.message, "edit_text"):
        with contextlib.suppress(Exception):
            await callback.message.edit_text(
                "Suggestion d'auto-tuning ignorée.\n\n"
                "Relance une analyse à tout moment avec /autotune.",
                reply_markup=None,
            )


@router.callback_query(F.data == "ai_tune_hub")
async def handle_tune_hub_callback(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Réaffiche la liste mise à jour des alertes avec faux positifs."""
    await callback.answer()
    await show_autotune_hub(callback, db_user.id, db_session)


@router.callback_query(F.data == "ai_tune_close")
async def handle_tune_close(callback: CallbackQuery) -> None:
    """Ferme le panneau Auto-Tuning."""
    await callback.answer()
    if callback.message and hasattr(callback.message, "delete"):
        with contextlib.suppress(Exception):
            await callback.message.delete()


@router.callback_query(F.data.startswith("ai_var_apply:"))
async def handle_var_apply(
    callback: CallbackQuery,
    db_user: User,
    db_session: Any,
) -> None:
    """Applique les variantes orthographiques suggérées à l'alerte."""
    parts = (callback.data or "").split(":", 1)
    if len(parts) < 2:
        return
    try:
        alert_id = uuid.UUID(parts[1])
    except ValueError:
        return

    pending = _pending_variants.get(str(alert_id))
    if pending is None or not pending.values:
        await callback.answer("Les suggestions ont expiré.", show_alert=True)
        return

    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        # L'alerte est introuvable : les variantes restent en mémoire, une
        # validation ultérieure n'a pas à déclencher une nouvelle analyse.
        await callback.answer("Alerte introuvable.", show_alert=True)
        return
    if pending.snapshot is not None and await alert_repo.lock_bibliographic_result(
        alert_id, pending.snapshot
    ) is None:
        await db_session.rollback()
        await callback.answer("Les suggestions ont expiré.", show_alert=True)
        return
    if _pending_variants.get(str(alert_id)) is not pending:
        await db_session.rollback()
        await callback.answer("Les suggestions ont expiré.", show_alert=True)
        return

    current_variants = list(alert.variants or [])
    for v in sanitize_variants(
        pending.values, alert.name, references=alert.include_keywords or ()
    ):
        if v.lower() not in [k.lower() for k in current_variants]:
            current_variants.append(v)

    alert.variants = current_variants
    await db_session.commit()
    pop_pending_variants(alert_id)

    await callback.answer("Variantes ajoutées.", show_alert=True)
    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=BTN_BACK_TO_ALERT, callback_data=f"al_edit:{alert.id}"
                )
            ]
        ]
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(
            f"✅ <b>Variantes ajoutées</b>\n\n"
            f"{FIELD_NAME} : « {escape_html(alert.name)} »\n"
            f"{FIELD_VARIANTS} : <code>{escape_html(', '.join(current_variants))}</code>",
            reply_markup=back_kb,
        )
