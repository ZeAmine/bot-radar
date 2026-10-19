"""Handlers Telegram d'approbation et de suspension de la maintenance.

Ce module ne corrige rien et ne déploie rien. Il enregistre une décision humaine,
ou suspend la maintenance. L'`alert_id` porté par le bouton lie chaque décision à
un incident précis : un bouton ne peut donc jamais autoriser autre chose que
l'incident qu'il accompagne.

L'autorisation est réputée *enregistrée*, pas exécutée : aucun exécuteur de
correction n'est branché à ce stade, et le message le dit explicitement.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.core.security import is_telegram_user_allowed
from app.services import alert_chain_state as state

logger = logging.getLogger(__name__)

router = Router(name="alert_chain_router")

APPROVE_PREFIX = "alert_chain:approve:"
REFUSE_PREFIX = "alert_chain:refuse:"

AUTHORISATION_LABEL = (
    "Autoriser l'analyse et la préparation d'un correctif — sans merge ni déploiement"
)

NOT_ENABLED_NOTICE = (
    "✅ <b>Autorisation enregistrée.</b>\n\n"
    "Aucune correction n'est lancée automatiquement : la préparation d'un correctif "
    "n'est pas encore branchée. Le merge et le déploiement resteront manuels."
)

REFUSED_NOTICE = (
    "⛔ <b>Refus enregistré.</b>\n\n"
    "L'incident reste ouvert : un correctif n'est jamais synonyme de clôture."
)

OUT_OF_SCOPE_NOTICE = "⛔ Décision refusée : cet incident n'est plus ouvert."


def approval_keyboard(alert_id: str) -> InlineKeyboardMarkup:
    """Clavier d'approbation, chaque bouton strictement lié à cet `alert_id`."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Autoriser l'analyse et la préparation d'un correctif",
                    callback_data=f"{APPROVE_PREFIX}{alert_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⛔ Refuser",
                    callback_data=f"{REFUSE_PREFIX}{alert_id}",
                )
            ],
        ]
    )


def _state_dir() -> Path:
    return state.state_dir()


def _authorized(user_id: int | None) -> bool:
    return user_id is not None and is_telegram_user_allowed(user_id)


async def _answer(callback: CallbackQuery, text: str, *, alert: bool = True) -> None:
    with contextlib.suppress(Exception):
        await callback.answer(text, show_alert=alert)


@router.callback_query(F.data.startswith(APPROVE_PREFIX))
async def handle_approve(callback: CallbackQuery) -> None:
    """Enregistre une autorisation, sous contrôle d'allowlist et d'état."""
    user = callback.from_user
    if not _authorized(user.id if user else None):
        await _answer(callback, "⛔ Action réservée aux utilisateurs autorisés.")
        return

    alert_id = (callback.data or "")[len(APPROVE_PREFIX) :]
    if not alert_id:
        await _answer(callback, "⛔ Demande incomplète.")
        return

    result = state.request_approval(
        directory=_state_dir(),
        alert_id=alert_id,
        user_id=user.id if user else 0,
        now=datetime.now(UTC),
        ttl_seconds=state.DEFAULT_APPROVAL_TTL_SECONDS,
    )

    if result.outcome is state.DecisionOutcome.RECORDED:
        await _answer(callback, NOT_ENABLED_NOTICE)
    elif result.outcome is state.DecisionOutcome.ALREADY_DECIDED:
        await _answer(callback, f"⛔ Rien à faire : {result.reason}.")
    elif result.outcome is state.DecisionOutcome.EXPIRED:
        await _answer(callback, f"⛔ {result.reason}.")
    elif result.reason in {"maintenance suspendue", "état de maintenance incertain"}:
        await _answer(callback, f"⛔ Décision refusée : {result.reason}.")
    else:
        await _answer(callback, OUT_OF_SCOPE_NOTICE)

    logger.info(
        "Décision d'approbation traitée",
        extra={
            "event": "alert_chain_decision",
            "alert_id": alert_id,
            "outcome": result.outcome.value,
            "user_id": user.id if user else None,
        },
    )


@router.callback_query(F.data.startswith(REFUSE_PREFIX))
async def handle_refuse(callback: CallbackQuery) -> None:
    """Enregistre un refus explicite ; l'incident reste ouvert."""
    user = callback.from_user
    if not _authorized(user.id if user else None):
        await _answer(callback, "⛔ Action réservée aux utilisateurs autorisés.")
        return

    alert_id = (callback.data or "")[len(REFUSE_PREFIX) :]
    if not alert_id:
        await _answer(callback, "⛔ Demande incomplète.")
        return

    result = state.refuse_approval(
        directory=_state_dir(),
        alert_id=alert_id,
        user_id=user.id if user else 0,
        now=datetime.now(UTC),
    )

    if result.outcome is state.DecisionOutcome.RECORDED:
        await _answer(callback, REFUSED_NOTICE)
    elif result.outcome is state.DecisionOutcome.ALREADY_DECIDED:
        await _answer(callback, f"⛔ Rien à faire : {result.reason}.")
    else:
        await _answer(callback, OUT_OF_SCOPE_NOTICE)


@router.message(Command("maintenance_stop"))
async def handle_stop(message: Message) -> None:
    """Suspend la maintenance : le bot métier et ses dépendances continuent."""
    user = message.from_user
    if not _authorized(user.id if user else None):
        await message.answer("⛔ Action réservée aux utilisateurs autorisés.")
        return
    state.suspend_maintenance(
        directory=_state_dir(),
        user_id=user.id if user else None,
        now=datetime.now(UTC),
        reason="suspension demandée par l'opérateur",
    )
    await message.answer(
        "⏸ <b>Maintenance suspendue.</b>\n\n"
        "Les nouveaux traitements de maintenance sont arrêtés et les incidents "
        "ouverts sont conservés. Le bot métier et ses dépendances ne sont pas "
        "touchés.\n\n"
        "La reprise exige <code>/maintenance_resume</code>. Pour arrêter aussi le "
        "déclencheur système : "
        "<code>sudo systemctl stop botradar-alert-chain.timer</code>."
    )


@router.message(Command("maintenance_resume"))
async def handle_resume(message: Message) -> None:
    """Reprend la maintenance, sur décision explicite uniquement."""
    user = message.from_user
    if not _authorized(user.id if user else None):
        await message.answer("⛔ Action réservée aux utilisateurs autorisés.")
        return
    state.resume_maintenance(
        directory=_state_dir(),
        user_id=user.id if user else None,
        now=datetime.now(UTC),
        reason="reprise demandée par l'opérateur",
    )
    await message.answer(
        "▶️ <b>Maintenance reprise.</b>\n\n"
        "Le détecteur reprend son contrôle au prochain déclenchement."
    )


@router.message(Command("maintenance_status"))
async def handle_status(message: Message) -> None:
    """Rend l'état de maintenance lisible, sans exposer de donnée sensible."""
    user = message.from_user
    if not _authorized(user.id if user else None):
        await message.answer("⛔ Action réservée aux utilisateurs autorisés.")
        return
    directory = _state_dir()
    try:
        state.validate_authority(directory)
        suspended = state.is_suspended(directory)
    except (OSError, ValueError):
        await message.answer(
            "⚠️ État de maintenance illisible. Une vérification opérateur est nécessaire."
        )
        return
    incident = state.find_open_incident(directory)
    failures = state.consecutive_failures(directory)
    decisions = state.read_decisions(directory)

    lines = [
        "<b>Maintenance de la chaîne d'alerte</b>",
        f"<b>Suspension :</b> {'oui' if suspended else 'non'}",
        f"<b>Incident ouvert :</b> {incident.alert_id if incident else 'aucun'}",
        f"<b>Contrôles impossibles consécutifs :</b> {failures}",
    ]
    if incident is not None:
        decision = decisions.get(incident.alert_id)
        lines.append(
            f"<b>Décision :</b> {decision.kind.value if decision else 'aucune'}"
        )
    await message.answer("\n".join(lines))


__all__: list[str] = ["AUTHORISATION_LABEL", "router"]
