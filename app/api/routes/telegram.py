"""Endpoint de réception du Webhook Telegram."""

import contextlib
import logging
import uuid
from typing import Any

from aiogram.types import Update
from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.bot.bot_instance import bot, dp
from app.core.security import verify_webhook_secret
from app.domain.entities.alert import PauseActionProgress

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/telegram", tags=["Telegram Webhook"])


@router.post("/webhook/{webhook_secret}")
async def handle_telegram_webhook(
    webhook_secret: str,
    raw_update: dict[str, Any],
    response: Response,
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
            pause_progress.update_id = update.update_id
            pause_progress.callback_id = callback.id
            parts = (callback.data or "").split(":")
            if len(parts) == 2 and parts[1].startswith("p_"):
                with contextlib.suppress(ValueError):
                    pause_progress.token = uuid.UUID(parts[1][2:])
            await dp.feed_update(bot=bot, update=update, pause_progress=pause_progress)
        else:
            await dp.feed_update(bot=bot, update=update)
    except Exception as exc:
        if pause_progress is not None:
            if pause_progress.effect == "unknown" and pause_progress.stage == "commit":
                from app.use_cases.pause_alert import reconcile

                await reconcile(pause_progress)
            logger.error(
                "Échec du parcours de pause ; état métier distinct du traitement restant.",
                extra={
                    "event": "pause_action_failed", "action": pause_progress.effect,
                    "stage": pause_progress.stage, "error_type": type(exc).__name__,
                    "result": pause_progress.result,
                    "current_status": pause_progress.current_status,
                },
            )
            if pause_progress.effect == "committed":
                if pause_progress.result == "replay":
                    return {"status": "ok", "action": "committed", "result": "reconciled",
                            "current_status": pause_progress.current_status,
                            "post_action": "not_attempted"}
                return {"status": "ok", "action": "committed", "post_action": "error"}
            if (
                pause_progress.token is not None
                and pause_progress.stage in {"dispatch", "lookup", "mutation"}
                and pause_progress.effect in {"not_started", "not_applied"}
                and is_transient_database_failure(exc)
            ):
                response.status_code = 503
                return {
                    "status": "error", "action": pause_progress.effect,
                    "message": "Traitement temporairement indisponible",
                }
            error_response = {
                "status": "error", "action": pause_progress.effect,
                "message": "Erreur interne traitée",
            }
            if pause_progress.result in {"superseded", "unknown"}:
                error_response["result"] = pause_progress.result
                error_response["current_status"] = pause_progress.current_status
            return error_response
        logger.exception("Erreur lors du traitement de l'update Telegram: %s", exc)
        # On retourne HTTP 200 à Telegram pour éviter les renvois en boucle d'updates erronées
        return {"status": "error", "message": "Erreur interne traitée"}

    if pause_progress is not None and pause_progress.effect != "not_started":
        logger.info(
            "Résultat métier du parcours de pause, sans preuve de réception utilisateur.",
            extra={"event": "pause_action_finished", "action": pause_progress.effect,
                   "stage": pause_progress.stage},
        )
        body = {"status": "error" if pause_progress.effect == "unknown" else "ok",
                    "action": pause_progress.effect}
        if pause_progress.result in {
            "replay", "consumed", "superseded", "expired", "unknown", "legacy",
        }:
            body["result"] = pause_progress.result
            body["current_status"] = pause_progress.current_status
        return body
    return {"status": "ok"}


def is_transient_database_failure(exc: Exception) -> bool:
    """Liste fermée, utilisée uniquement avec une frontière métier sans commit tenté."""
    if isinstance(exc, PoolTimeoutError):
        return True
    if isinstance(exc, OperationalError):
        code = getattr(exc.orig, "sqlstate", None)
        return bool(exc.connection_invalidated or code in {
            "40001", "40P01", "55P03", "08006", "08001",
        })
    return False
