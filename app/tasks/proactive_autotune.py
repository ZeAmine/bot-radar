"""Tâche de fond d'Auto-Tuning proactif des alertes (Phase A).

Analyse les motifs de faux positifs répétés et soumet automatiquement
à l'utilisateur des propositions d'exclusion pertinentes prêtes à valider en 1 clic.
"""

import asyncio
import contextlib
import logging
import uuid

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.labels import (
    BTN_APPLY_EXCLUSIONS,
    BTN_EDIT_EXCLUSIONS,
    BTN_IGNORE_EXCLUSIONS,
    exclusion_proposal_message,
)
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

    # Créneau réservé AVANT l'appel au moteur (`SET NX` atomique) : deux analyses
    # concurrentes de la même alerte ne peuvent pas produire deux propositions.
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

            # Mise en cache pour la validation en un clic.
            await save_pending_tuning(alert.id, new_exclusions)

        # Gabarit partagé : scan proactif et analyse demandée disent la même chose,
        # sans explication qui reformule la liste des faux positifs.
        text = exclusion_proposal_message(alert.name, disliked_titles, new_exclusions)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=BTN_APPLY_EXCLUSIONS,
                        callback_data=f"ai_tune_apply:{alert.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=BTN_EDIT_EXCLUSIONS,
                        callback_data=f"ai_tune_edit:{alert.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=BTN_IGNORE_EXCLUSIONS,
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
        # Rien émis : créneau relâché pour permettre un nouvel essai. Une proposition
        # réellement envoyée le garde 24 h, sans répétition à chaque scan.
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
            # Délai pour éviter le spam Telegram.
            await asyncio.sleep(1.0)

    logger.info("Scan Auto-Tuning proactif terminé : %d alerte(s) notifiée(s)", notified_count)
    return notified_count
