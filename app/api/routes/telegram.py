"""Endpoint de réception du Webhook Telegram."""

import logging
from typing import Any

from aiogram.types import Update
from fastapi import APIRouter, HTTPException, status

from app.bot.bot_instance import bot, dp
from app.core.security import verify_webhook_secret
from app.domain.entities.alert import PauseActionProgress

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/telegram", tags=["Telegram Webhook"])


@router.post("/webhook/{webhook_secret}")
async def handle_telegram_webhook(
    webhook_secret: str,
    raw_update: dict[str, Any],
) -> dict[str, str]:
    """Reçoit et traite les updates Telegram de manière sécurisée."""
    if not verify_webhook_secret(webhook_secret):
        logger.warning("Accès non autorisé au webhook Telegram avec un secret invalide.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Secret de webhook invalide.",
        )

    pause_progress: PauseActionProgress | None = None
    try:
        update = Update.model_validate(raw_update, context={"bot": bot})
        callback = update.callback_query
        if callback is not None and (callback.data or "").startswith("al_pause:"):
            # Objet serveur propre à cet appel, jamais issu du JSON ou d'un singleton.
            pause_progress = PauseActionProgress()
            await dp.feed_update(bot=bot, update=update, pause_progress=pause_progress)
        else:
            await dp.feed_update(bot=bot, update=update)
    except Exception as exc:
        if pause_progress is not None:
            logger.error(
                "Échec du parcours de pause ; état métier distinct du traitement restant.",
                extra={
                    "event": "pause_action_failed", "action": pause_progress.effect,
                    "stage": pause_progress.stage, "error_type": type(exc).__name__,
                },
            )
            if pause_progress.effect == "committed":
                return {"status": "ok", "action": "committed", "post_action": "error"}
            return {
                "status": "error", "action": pause_progress.effect,
                "message": "Erreur interne traitée",
            }
        logger.exception("Erreur lors du traitement de l'update Telegram: %s", exc)
        # On retourne HTTP 200 à Telegram pour éviter les renvois en boucle d'updates erronées
        return {"status": "error", "message": "Erreur interne traitée"}

    if pause_progress is not None and pause_progress.effect != "not_started":
        logger.info(
            "Résultat métier du parcours de pause, sans preuve de réception utilisateur.",
            extra={"event": "pause_action_finished", "action": pause_progress.effect,
                   "stage": pause_progress.stage},
        )
        return {"status": "ok", "action": pause_progress.effect}
    return {"status": "ok"}
