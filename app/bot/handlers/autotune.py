"""Handler pour la commande /autotune et l'Auto-Tuning proactif des alertes (Use Case G)."""

import contextlib
import html
import logging
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.states.alert_creation import AutoTuneState
from app.db.models.alert import Alert
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.autotune_repo import (
    clear_dislikes_for_alert,
    get_alerts_with_dislikes,
    get_disliked_titles_for_alert,
)
from app.services.ai.modules.auto_tuning import auto_tuning_module

logger = logging.getLogger(__name__)

router = Router(name="autotune_router")

# Cache mémoire court pour les suggestions en attente de confirmation (évite dépassement 64B)
_pending_tuning: dict[str, list[str]] = {}
_pending_variants: dict[str, list[str]] = {}
_active_tuning_alerts: set[str] = set()


async def show_autotune_hub(
    target: Message | CallbackQuery,
    user_id: uuid.UUID,
    db_session: Any,
) -> None:
    """Affiche ou met à jour la liste des alertes avec faux positifs à traiter."""
    alerts_with_dislikes = await get_alerts_with_dislikes(user_id, db_session)

    if not alerts_with_dislikes:
        empty_text = (
            "🧹 <b>Auto-Tuning des Alertes (IA)</b>\n\n"
            "✨ <b>Toutes vos alertes sont propres !</b>\n"
            "Aucun faux positif signalé pour le moment.\n\n"
            "💡 <i>Comment ça marche ?</i>\n"
            "Lorsqu'une alerte vous envoie une annonce hors-sujet, cliquez sur le bouton "
            "<b>[👎 Faux positif]</b> sous le message.\n"
            "Ensuite, relancez <code>/autotune</code> : l'IA analysera les parasites "
            "pour vous suggérer des exclusions automatiques en 1 clic."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Fermer", callback_data="ai_tune_close")]
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
                text=f"🎯 {alert.name} ({count} faux positif{'s' if count > 1 else ''})",
                callback_data=f"ai_tune_select:{alert.id}",
            )
        ]
        for alert, count in alerts_with_dislikes
    ]
    buttons.append([InlineKeyboardButton(text="❌ Annuler", callback_data="ai_tune_close")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    hub_text = (
        "🧹 <b>Auto-Tuning des Alertes (IA)</b>\n\n"
        "Sélectionnez l'alerte à analyser pour nettoyer ses faux positifs :"
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
            "⏸️ <b>L'Auto-Tuning est actuellement désactivé</b> dans la configuration."
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
                f"Aucune alerte trouvée correspondant à « {html.escape(target_query)} »."
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
            await target.answer("⏳ Une analyse Auto-Tuning est déjà en cours...", show_alert=True)
        return

    _active_tuning_alerts.add(alert_key)
    loading_text = (
        f"🧹 <b>Auto-Tuning : « {html.escape(alert.name)} »</b>\n\n"
        "⏳ <b>Analyse IA en cours...</b>\n"
        "<i>Recherche des faux positifs et analyse par DeepSeek, veuillez patienter...</i>"
    )
    loading_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Analyse en cours...", callback_data="noop")]
        ]
    )

    if isinstance(target, CallbackQuery):
        await target.answer()
        if target.message and hasattr(target.message, "edit_text"):
            await target.message.edit_text(loading_text, reply_markup=loading_kb)
    else:
        await target.answer(loading_text, reply_markup=loading_kb)

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← Retour à la liste Auto-Tuning", callback_data="ai_tune_hub"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ Modifier l'alerte", callback_data=f"al_edit:{alert.id}"
                )
            ],
        ]
    )

    try:
        disliked_titles = await get_disliked_titles_for_alert(alert.id, db_session)
        if not disliked_titles:
            msg = (
                f"🧹 <b>Auto-Tuning : « {html.escape(alert.name)} »</b>\n\n"
                "✨ <b>Aucun faux positif enregistré pour cette alerte.</b>\n\n"
                "Pour que l'Auto-Tuning fonctionne et apprenne de vos préférences :\n"
                "• Cliquez sur le bouton <b>[👎 Faux positif]</b> sous les alertes indésirables.\n"
                "• L'IA analysera les parasites pour vous suggérer des exclusions automatiques !"
            )
            if isinstance(target, CallbackQuery):
                if target.message and hasattr(target.message, "edit_text"):
                    await target.message.edit_text(msg, reply_markup=back_kb)
            else:
                await target.answer(msg, reply_markup=back_kb)
            return

        exclusions, explanation = await auto_tuning_module.suggest_exclusions(
            alert.name, disliked_titles
        )

        existing_excludes = [e.lower() for e in (alert.exclude_keywords or [])]
        new_exclusions = [e for e in exclusions if e.lower() not in existing_excludes]

        titles_bullet = "\n".join(f"• <i>{html.escape(t)}</i>" for t in disliked_titles[:5])

        if not new_exclusions:
            # Purger les faux positifs résolus déjà couverts par les exclusions existantes
            await clear_dislikes_for_alert(alert.id, db_session)
            await db_session.commit()

            active_excl_str = (
                ", ".join(alert.exclude_keywords) if alert.exclude_keywords else "Aucune"
            )
            res_text = (
                f"🧹 <b>Auto-Tuning : « {html.escape(alert.name)} »</b>\n\n"
                f"💡 <b>L'IA a analysé {len(disliked_titles)} annonce(s) indésirable(s) :</b>\n"
                f"{titles_bullet}\n\n"
                f"📋 <b>Exclusions actives :</b> <code>{html.escape(active_excl_str)}</code>\n\n"
                "✅ Toutes les exclusions pertinentes de l'IA sont déjà actives sur l'alerte !\n"
                "🗑 <i>Les faux positifs traités ont été retirés de la liste.</i>"
            )
            if isinstance(target, CallbackQuery):
                if target.message and hasattr(target.message, "edit_text"):
                    await target.message.edit_text(res_text, reply_markup=back_kb)
            else:
                await target.answer(res_text, reply_markup=back_kb)
            return

        # Mettre en cache pour la confirmation
        _pending_tuning[str(alert.id)] = new_exclusions

        excl_str = ", ".join(new_exclusions)
        exp_text = f"\n💡 <i>{html.escape(explanation)}</i>\n" if explanation else ""

        text = (
            f"🧹 <b>Auto-Tuning : « {html.escape(alert.name)} »</b>\n\n"
            f"📊 <b>Analyse de {len(disliked_titles)} faux positif(s) :</b>\n"
            f"{titles_bullet}\n"
            f"{exp_text}\n"
            f"🎯 <b>Nouvelles exclusions suggérées :</b>\n"
            f"<code>{html.escape(excl_str)}</code>\n\n"
            "Voulez-vous appliquer ces exclusions à votre alerte ?"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Appliquer les exclusions",
                        callback_data=f"ai_tune_apply:{alert.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ Modifier la liste",
                        callback_data=f"ai_tune_edit:{alert.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="← Annuler",
                        callback_data=f"ai_tune_cancel:{alert.id}",
                    )
                ],
            ]
        )

        if isinstance(target, CallbackQuery):
            if target.message and hasattr(target.message, "edit_text"):
                await target.message.edit_text(text, reply_markup=keyboard)
        else:
            await target.answer(text, reply_markup=keyboard)

    except Exception as err:
        logger.error("Erreur lors de l'Auto-Tuning pour %s: %s", alert.name, err)
        err_text = (
            f"⚠️ Une erreur est survenue lors de l'analyse IA pour « {html.escape(alert.name)} ».\n"
            "Veuillez réessayer dans un instant."
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

    exclusions = _pending_tuning.pop(str(alert_id), [])
    if not exclusions:
        await callback.answer("Les suggestions ont expiré. Relancez /autotune.", show_alert=True)
        return

    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    current_excludes = list(alert.exclude_keywords or [])
    for exc in exclusions:
        if exc.lower() not in [e.lower() for e in current_excludes]:
            current_excludes.append(exc)

    alert.exclude_keywords = current_excludes

    # Suppression des faux positifs résolus pour cette alerte
    cleaned_count = await clear_dislikes_for_alert(alert.id, db_session)
    await db_session.commit()

    await callback.answer("✅ Exclusions appliquées avec succès !", show_alert=True)
    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← Retour à la liste Auto-Tuning", callback_data="ai_tune_hub"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ Modifier l'alerte", callback_data=f"al_edit:{alert.id}"
                )
            ],
        ]
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        cleanup_note = (
            f"🗑 <i>{cleaned_count} faux positif{'s' if cleaned_count > 1 else ''} "
            "traité(s) retiré(s) de la liste.</i>"
            if cleaned_count > 0
            else "🗑 <i>Les faux positifs traités ont été retirés de la liste.</i>"
        )
        await callback.message.edit_text(
            f"✅ <b>Alerte « {html.escape(alert.name)} » mise à jour !</b>\n\n"
            f"🎯 Nouvelles exclusions actives : <code>{', '.join(current_excludes)}</code>\n\n"
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

    current_suggestions = _pending_tuning.get(str(alert.id), [])
    if not current_suggestions:
        await callback.answer("Les suggestions ont expiré. Relancez /autotune.", show_alert=True)
        return

    await state.set_state(AutoTuneState.waiting_for_custom_exclusions)
    await state.update_data(tuning_alert_id=str(alert.id))

    copy_text = ", ".join(current_suggestions)
    prompt_text = (
        f"✏️ <b>Personnaliser les exclusions pour « {html.escape(alert.name)} »</b>\n\n"
        "1. <b>Copiez</b> les suggestions en appuyant sur le bloc ci-dessous :\n"
        f"<code>{html.escape(copy_text)}</code>\n\n"
        "2. <b>Modifiez</b> le texte selon vos souhaits (ajoutez, retirez ou ajustez des termes "
        "séparés par des virgules ou retours à la ligne).\n\n"
        "3. <b>Envoyez</b> votre nouveau message dans ce chat.\n\n"
        "<i>Tapez /cancel pour annuler la modification.</i>"
    )

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← Revenir sans modifier",
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

    suggestions = _pending_tuning.get(str(alert.id), [])
    if not suggestions:
        await callback.answer("Les suggestions ont expiré.", show_alert=True)
        await show_autotune_hub(callback, db_user.id, db_session)
        return

    excl_str = ", ".join(suggestions)
    text = (
        f"🧹 <b>Auto-Tuning : « {html.escape(alert.name)} »</b>\n\n"
        "🎯 <b>Nouvelles exclusions suggérées :</b>\n"
        f"<code>{html.escape(excl_str)}</code>\n\n"
        "Voulez-vous appliquer ces exclusions à votre alerte ?"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Appliquer les exclusions",
                    callback_data=f"ai_tune_apply:{alert.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Modifier la liste",
                    callback_data=f"ai_tune_edit:{alert.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="← Annuler",
                    callback_data=f"ai_tune_cancel:{alert.id}",
                )
            ],
        ]
    )
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
            _pending_tuning.pop(alert_id_str, None)
        await message.answer("❌ Modification annulée. Tapez /autotune pour recommencer.")
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
            "⚠️ <b>Aucun mot-clé valide détecté.</b>\n"
            "Veuillez envoyer au moins un mot-clé (séparé par une virgule ou "
            "un retour à la ligne), ou tapez /cancel pour annuler."
        )
        return

    _pending_tuning[alert_id_str] = parsed
    await state.clear()

    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(uuid.UUID(alert_id_str), user_id=db_user.id)
    alert_name = alert.name if alert else "Alerte"

    excl_str = ", ".join(parsed)
    text = (
        f"🧹 <b>Auto-Tuning : « {html.escape(alert_name)} »</b>\n\n"
        f"🎯 <b>Nouvelles exclusions à appliquer (personnalisées) :</b>\n"
        f"<code>{html.escape(excl_str)}</code>\n\n"
        "Voulez-vous appliquer cette liste d'exclusions à votre alerte ?"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Appliquer les exclusions",
                    callback_data=f"ai_tune_apply:{alert_id_str}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Modifier à nouveau",
                    callback_data=f"ai_tune_edit:{alert_id_str}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="← Annuler",
                    callback_data=f"ai_tune_cancel:{alert_id_str}",
                )
            ],
        ]
    )
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
        _pending_tuning.pop(alert_id, None)
    await callback.answer("Opération annulée.")

    if not alert_id:
        await show_autotune_hub(callback, db_user.id, db_session)
        return

    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← Retour à la liste Auto-Tuning", callback_data="ai_tune_hub"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ Modifier l'alerte", callback_data=f"al_edit:{alert_id}"
                )
            ],
        ]
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(
            "❌ <b>Auto-Tuning annulé.</b>\n\n"
            "<i>Les faux positifs sont conservés pour une analyse ultérieure.</i>",
            reply_markup=back_kb,
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

    variants = _pending_variants.pop(str(alert_id), [])
    if not variants:
        await callback.answer("Les suggestions ont expiré.", show_alert=True)
        return

    alert_repo = AlertRepository(db_session)
    alert = await alert_repo.get_by_id(alert_id, user_id=db_user.id)
    if not alert:
        await callback.answer("Alerte introuvable.", show_alert=True)
        return

    current_variants = list(alert.variants or [])
    for v in variants:
        if v.lower() not in [k.lower() for k in current_variants]:
            current_variants.append(v)

    alert.variants = current_variants
    await db_session.commit()

    await callback.answer("✅ Variantes ajoutées avec succès !", show_alert=True)
    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← Retour à l'alerte", callback_data=f"al_edit:{alert.id}"
                )
            ]
        ]
    )
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(
            f"✅ <b>Alerte « {html.escape(alert.name)} » mise à jour !</b>\n\n"
            f"✨ Variantes actives : <code>{', '.join(current_variants)}</code>",
            reply_markup=back_kb,
        )
