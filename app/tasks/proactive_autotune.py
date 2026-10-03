"""Tâche de fond d'Auto-Tuning proactif des alertes (Phase A).

Analyse les motifs de faux positifs répétés et soumet automatiquement
à l'utilisateur des propositions d'exclusion pertinentes prêtes à valider en 1 clic.
"""

import asyncio
import contextlib
import html
import logging
import uuid

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.constants import AlertStatus
from app.core.tasks import safe_create_task
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.autotune_repo import (
    get_all_alerts_with_dislike_threshold,
    get_disliked_titles_for_alert,
)
from app.db.session import AsyncSessionLocal
from app.services.ai.modules.auto_tuning import (
    auto_tuning_module,
    build_exclusion_proposal,
    claim_autotune_slot,
    release_autotune_claim,
    save_pending_tuning,
)

logger = logging.getLogger("radar.proactive_autotune")


def _get_default_bot() -> Bot:
    """Fournit l'instance singleton du bot Telegram (import paresseux)."""
    from app.bot.bot_instance import bot

    return bot


async def check_and_trigger_proactive_autotune(
    alert_id: uuid.UUID,
    bot: Bot,
    chat_id: int,
) -> bool:
    """Analyse les faux positifs d'une alerte et envoie une proposition proactive si pertinent."""
    if not auto_tuning_module.is_enabled:
        return False

    # Le créneau d'émission est réservé AVANT l'appel au moteur : `SET NX` est
    # atomique, donc deux analyses concurrentes de la même alerte (scan planifié et
    # clic « Faux positif ») ne peuvent pas produire deux propositions. Le créneau
    # est relâché si rien n'a finalement été émis.
    if not await claim_autotune_slot(alert_id):
        logger.debug("Auto-Tuning proactif : créneau déjà réservé pour l'alerte %s", alert_id)
        return False

    emitted = False
    try:
        async with AsyncSessionLocal() as session:
            alert_repo = AlertRepository(session)
            alert = await alert_repo.get_by_id(alert_id)
            if not alert or alert.status != AlertStatus.ACTIVE:
                return False

            disliked_titles = await get_disliked_titles_for_alert(alert_id, session)

            try:
                proposal = await build_exclusion_proposal(
                    alert_name=alert.name,
                    already_excluded=list(alert.exclude_keywords or []),
                    disliked_titles=disliked_titles,
                )
            except Exception as exc:
                logger.warning(
                    "Échec de l'analyse IA proactive pour l'alerte %s: %s", alert.name, exc
                )
                return False

            if proposal is None:
                return False

            new_exclusions = proposal.exclusions
            explanation = proposal.explanation

            # Mettre en cache pour la validation 1-clic
            await save_pending_tuning(alert.id, new_exclusions)

        excl_str = ", ".join(new_exclusions)
        titles_bullet = "\n".join(f"• <i>{html.escape(t)}</i>" for t in disliked_titles[:3])
        exp_text = f"\n💡 <i>{html.escape(explanation)}</i>\n" if explanation else ""

        text = (
            f"🤖 <b>Auto-Tuning Proactif : « {html.escape(alert.name)} »</b>\n\n"
            f"Vous avez marqué {len(disliked_titles)} faux positifs récemment "
            "sur cette alerte :\n"
            f"{titles_bullet}\n"
            f"{exp_text}\n"
            f"🎯 <b>Nouvelles exclusions suggérées par l'IA :</b>\n"
            f"<code>{html.escape(excl_str)}</code>\n\n"
            "Voulez-vous appliquer ces exclusions pour assainir vos futures alertes ?"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Appliquer en 1 clic",
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
                        text="❌ Ignorer",
                        callback_data=f"ai_tune_dismiss:{alert.id}",
                    )
                ],
            ]
        )

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
            )
            emitted = True
            logger.info(
                "Proposition proactive d'Auto-Tuning envoyée pour '%s' (chat: %s)",
                alert.name,
                chat_id,
            )
            return True
        except Exception as exc:
            logger.error("Erreur lors de l'envoi de la proposition proactive: %s", exc)
            return False
    finally:
        # Rien n'a été émis (alerte inactive, moins de 2 dislikes, moteur muet,
        # rien de nouveau ou envoi en échec) : le créneau est relâché pour qu'une
        # analyse ultérieure puisse réessayer. Une proposition réellement envoyée
        # garde son créneau 24 h, ce qui évite de la répéter à chaque scan.
        if not emitted:
            await release_autotune_claim(alert_id)


async def _delayed_proactive_check(
    alert_id: uuid.UUID,
    bot: Bot,
    chat_id: int,
    delay_seconds: float = 2.5,
) -> None:
    """Attend quelques secondes après le clic dislike avant de vérifier et proposer."""
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.sleep(delay_seconds)
        await check_and_trigger_proactive_autotune(alert_id, bot, chat_id)


def schedule_proactive_autotune_check(
    alert_id: uuid.UUID,
    bot: Bot,
    chat_id: int,
) -> None:
    """Déclenche la vérification proactive en arrière-plan sans bloquer."""
    safe_create_task(
        _delayed_proactive_check(alert_id, bot, chat_id),
        name=f"delayed_proactive_autotune_{alert_id}",
    )


async def run_proactive_autotune_scan(bot: Bot | None = None) -> int:
    """Scanne toutes les alertes ayant >= 2 dislikes non résolus (pour job APScheduler)."""
    if not auto_tuning_module.is_enabled:
        return 0

    target_bot = bot or _get_default_bot()
    notified_count = 0

    async with AsyncSessionLocal() as session:
        alerts_to_tune = await get_all_alerts_with_dislike_threshold(
            min_dislikes=2,
            db_session=session,
        )

    for alert, user, _ in alerts_to_tune:
        if not user.telegram_user_id:
            continue
        success = await check_and_trigger_proactive_autotune(
            alert_id=alert.id,
            bot=target_bot,
            chat_id=user.telegram_user_id,
        )
        if success:
            notified_count += 1
            # Petit délai pour éviter le spam Telegram
            await asyncio.sleep(1.0)

    logger.info("Scan Auto-Tuning proactif terminé : %d alerte(s) notifiée(s)", notified_count)
    return notified_count
