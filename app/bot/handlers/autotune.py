"""Handler pour la commande /autotune et l'Auto-Tuning proactif des alertes (Use Case G)."""

import html
import logging
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.db.models.alert import Alert
from app.db.models.bot_event import BotEvent
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.services.ai.modules.auto_tuning import auto_tuning_module
from app.services.ai.modules.keyword_variants import keyword_variants_module

logger = logging.getLogger(__name__)

router = Router(name="autotune_router")

# Cache mémoire court pour les suggestions en attente de confirmation (évite dépassement 64B callback_data)
_pending_tuning: dict[str, list[str]] = {}
_pending_variants: dict[str, list[str]] = {}


async def get_disliked_titles_for_alert(alert_id: uuid.UUID, db_session: Any) -> list[str]:
    """Récupère les titres des annonces marquées comme faux positifs pour une alerte."""
    stmt = (
        select(Listing.title)
        .join(ListingMatch, ListingMatch.listing_id == Listing.id)
        .join(BotEvent, BotEvent.listing_id == Listing.id)
        .where(
            ListingMatch.alert_id == alert_id,
            BotEvent.event_type == "listing_disliked",
        )
        .order_by(BotEvent.created_at.desc())
        .limit(20)
    )
    res = await db_session.execute(stmt)
    titles = [row[0] for row in res.all() if row[0]]
    return titles


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

    alert_repo = AlertRepository(db_session)
    user_alerts = await alert_repo.list_by_user(db_user.id)
    if not user_alerts:
        await message.answer("Vous n'avez aucune alerte active. Utilisez /add pour en créer une.")
        return

    # Vérifier l'argument optionnel (nom d'alerte)
    args = (message.text or "").strip().split(maxsplit=1)
    target_query = args[1].strip().lower() if len(args) > 1 else None

    # Si une alerte spécifique est ciblée
    if target_query:
        matched_alerts = [a for a in user_alerts if target_query in a.name.lower()]
        if not matched_alerts:
            await message.answer(
                f"Aucune alerte trouvée correspondant à « {html.escape(target_query)} »."
            )
            return
        target_alert = matched_alerts[0]
        await process_autotune_for_alert(message, target_alert, db_session)
        return

    # Recherche globale : trouver les alertes ayant des faux positifs signalés
    alerts_with_dislikes: list[tuple[Alert, int]] = []
    for alert in user_alerts:
        disliked_titles = await get_disliked_titles_for_alert(alert.id, db_session)
        if disliked_titles:
            alerts_with_dislikes.append((alert, len(disliked_titles)))

    if not alerts_with_dislikes:
        await message.answer(
            "🧹 <b>Auto-Tuning des Alertes (IA)</b>\n\n"
            "✨ <b>Aucun faux positif signalé pour le moment !</b>\n\n"
            "💡 <i>Comment ça marche ?</i>\n"
            "Lorsqu'une alerte vous envoie une annonce hors-sujet, cliquez sur le bouton "
            "<b>[👎 Faux positif]</b> sous le message.\n"
            "Ensuite, lancez <code>/autotune</code> : l'IA analysera les annonces indésirables "
            "pour identifier les mots parasites et vous suggérer des exclusions ciblées en 1 clic."
        )
        return

    # Si une seule alerte a des faux positifs, la traiter directement
    if len(alerts_with_dislikes) == 1:
        target_alert, _ = alerts_with_dislikes[0]
        await process_autotune_for_alert(message, target_alert, db_session)
        return

    # Sinon, afficher le menu de sélection
    buttons = [
        [
            InlineKeyboardButton(
                text=f"🎯 {alert.name} ({count} faux positifs)",
                callback_data=f"ai_tune_select:{alert.id}",
            )
        ]
        for alert, count in alerts_with_dislikes
    ]
    buttons.append([InlineKeyboardButton(text="❌ Annuler", callback_data="ai_tune_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(
        "🧹 <b>Auto-Tuning des Alertes (IA)</b>\n\n"
        "Sélectionnez l'alerte à analyser pour nettoyer ses faux positifs :",
        reply_markup=keyboard,
    )


async def process_autotune_for_alert(
    target: Message | CallbackQuery,
    alert: Alert,
    db_session: Any,
) -> None:
    """Exécute l'analyse IA des faux positifs pour une alerte donnée."""
    disliked_titles = await get_disliked_titles_for_alert(alert.id, db_session)
    if not disliked_titles:
        msg = (
            f"🧹 <b>Auto-Tuning : « {html.escape(alert.name)} »</b>\n\n"
            "✨ Aucun faux positif enregistré pour cette alerte.\n"
            "<i>Signalez les annonces indésirables avec [👎 Faux positif] sous leurs alertes.</i>"
        )
        if isinstance(target, CallbackQuery):
            await target.answer()
            if target.message and hasattr(target.message, "edit_text"):
                await target.message.edit_text(msg)
        else:
            await target.answer(msg)
        return

    if isinstance(target, CallbackQuery):
        await target.answer("🤖 Analyse DeepSeek en cours...")

    exclusions, explanation = await auto_tuning_module.suggest_exclusions(
        alert.name, disliked_titles
    )

    # Filtrer les exclusions déjà présentes
    existing_excludes = [e.lower() for e in (alert.exclude_keywords or [])]
    new_exclusions = [e for e in exclusions if e.lower() not in existing_excludes]

    if not new_exclusions:
        res_text = (
            f"🧹 <b>Auto-Tuning : « {html.escape(alert.name)} »</b>\n\n"
            f"💡 L'IA a analysé {len(disliked_titles)} annonce(s) indésirable(s).\n"
            "Toutes les exclusions pertinentes sont déjà actives sur cette alerte !"
        )
        if isinstance(target, CallbackQuery):
            if target.message and hasattr(target.message, "edit_text"):
                await target.message.edit_text(res_text)
        else:
            await target.answer(res_text)
        return

    # Mettre en cache pour la confirmation
    _pending_tuning[str(alert.id)] = new_exclusions

    excl_str = ", ".join(new_exclusions)
    exp_text = f"\n💡 <i>{html.escape(explanation)}</i>\n" if explanation else ""

    text = (
        f"🧹 <b>Auto-Tuning : « {html.escape(alert.name)} »</b>\n\n"
        f"📊 <b>Analyse de {len(disliked_titles)} faux positif(s) :</b>"
        f"{exp_text}\n"
        f"🎯 <b>Exclusions suggérées :</b>\n"
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
                    text="❌ Ignorer",
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
    await db_session.commit()

    await callback.answer("✅ Exclusions appliquées avec succès !", show_alert=True)
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(
            f"✅ <b>Alerte « {html.escape(alert.name)} » mise à jour !</b>\n\n"
            f"🚫 Nouvelles exclusions actives : <code>{', '.join(current_excludes)}</code>"
        )


@router.callback_query(F.data.startswith("ai_tune_cancel"))
async def handle_tune_cancel(callback: CallbackQuery) -> None:
    """Annule l'opération d'auto-tuning."""
    parts = (callback.data or "").split(":", 1)
    if len(parts) > 1:
        _pending_tuning.pop(parts[1], None)
    await callback.answer("Opération annulée.")
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text("❌ Auto-tuning annulé.")


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

    current_includes = list(alert.include_keywords or [alert.name])
    for v in variants:
        if v.lower() not in [k.lower() for k in current_includes]:
            current_includes.append(v)

    alert.include_keywords = current_includes
    await db_session.commit()

    await callback.answer("✅ Variantes ajoutées avec succès !", show_alert=True)
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(
            f"✅ <b>Alerte « {html.escape(alert.name)} » mise à jour !</b>\n\n"
            f"🔤 Mots-clés inclus actifs : <code>{', '.join(current_includes)}</code>"
        )


@router.callback_query(F.data.startswith("ai_var_cancel:"))
async def handle_var_cancel(callback: CallbackQuery) -> None:
    """Annule l'ajout de variantes."""
    parts = (callback.data or "").split(":", 1)
    if len(parts) > 1:
        _pending_variants.pop(parts[1], None)
    await callback.answer("Opération annulée.")
    if callback.message and hasattr(callback.message, "edit_text"):
        await callback.message.edit_text("❌ Ajout des variantes annulé.")
