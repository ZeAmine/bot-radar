"""Endpoint de réception du Webhook Telegram."""

import logging
from typing import Any

from aiogram.types import Update
from fastapi import APIRouter, HTTPException, status

from app.bot.bot_instance import bot, dp
from app.core.security import verify_webhook_secret

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

    try:
        update = Update.model_validate(raw_update, context={"bot": bot})
        await dp.feed_update(bot=bot, update=update)
    except Exception as exc:
        logger.exception("Erreur lors du traitement de l'update Telegram: %s", exc)
        # On retourne HTTP 200 à Telegram pour éviter les renvois en boucle d'updates erronées
        return {"status": "error", "message": "Erreur interne traitée"}

    return {"status": "ok"}
