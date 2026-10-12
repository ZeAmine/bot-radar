"""Décision de pause protégée et réconciliation bornée, sans reprise de fond."""

import asyncio
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.alert import Alert
from app.db.models.pause_action import PauseAction
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.pause_action_repo import PauseActionRepository, utc
from app.db.session import AsyncSessionLocal
from app.domain.entities.alert import PauseActionProgress
from app.domain.entities.alert_preparation import preparation_request
from app.domain.enums import AlertStatus

Outcome = Literal[
    "ready", "applied", "replay", "consumed", "superseded", "expired", "refused", "unknown",
]


def classify(
    action: PauseAction | None,
    alert: Alert | None,
    update_id: int,
    callback_id: str,
    *,
    now: datetime | None = None,
) -> Outcome:
    if action is None:
        return "unknown"  # Une preuve absente/purgée n'autorise jamais sa recréation.
    if action.applied_at is not None:
        return (
            "replay"
            if (action.update_id, action.callback_id)
            == (
                update_id,
                callback_id,
            )
            else "consumed"
        )
    if action.invalidated:
        return "unknown"  # Restauration : ne rien inférer de l'absence d'application.
    if alert is None or alert.user_id != action.user_id or alert.deleted_at is not None:
        return "refused"
    if alert.status == AlertStatus.DELETED:
        return "refused"
    if alert.decision_version != action.expected_version:
        return "superseded"
    if utc(action.expires_at) <= (now or datetime.now(UTC)):
        return "expired"
    request = preparation_request(alert)
    if action.intent == "pause":
        return "ready" if alert.status == AlertStatus.ACTIVE and request is None else "refused"
    if action.intent == "suspend_preparation":
        return "ready" if (
            request is not None and request.activation_requested
            and request.state == "pending" and alert.status == AlertStatus.PAUSED
        ) else "refused"
    return "refused"


async def reconcile(progress: PauseActionProgress) -> None:
    """Une tentative, après résolution/fermeture de la session métier, sans mutation."""
    if progress.token is None or progress.user_id is None:
        return
    try:
        async with asyncio.timeout(2):
            async with AsyncSessionLocal() as session:
                action, alert = await PauseActionRepository(session).acquire(
                    progress.token,
                    progress.user_id,
                )
                result = classify(action, alert, progress.update_id, progress.callback_id)
                current_status = alert.status.value if alert is not None else "missing"
                if result == "replay":
                    # La preuve acquise reste vraie même si la fermeture échoue ensuite.
                    progress.effect = "committed"
                    progress.result = result
                    progress.current_status = current_status
            if progress.effect != "committed":
                progress.result = result
                progress.current_status = current_status
                if result == "ready":
                    progress.effect = "not_applied"
                # Un conflit plus récent n'est pas une preuve de non-application passée.
    except Exception:
        if progress.effect != "committed":
            progress.effect = "unknown"
            progress.result = "unknown"
    # CancelledError se propage, même pendant la relecture.


async def apply_pause(
    session: AsyncSession,
    progress: PauseActionProgress,
) -> Outcome:
    assert progress.token is not None
    assert progress.user_id is not None
    progress.stage = "lookup"
    action, alert = await PauseActionRepository(session).acquire(progress.token, progress.user_id)
    result = classify(action, alert, progress.update_id, progress.callback_id)
    progress.result = result
    progress.current_status = alert.status.value if alert is not None else "missing"
    if result != "ready":
        progress.effect = (
            "committed"
            if result == "replay"
            else ("unknown" if result == "unknown" else "not_applied")
        )
        # Relâcher les verrous avant toute présentation. Pas de nouveau commit métier.
        await session.rollback()
        return result
    assert action is not None
    assert alert is not None
    progress.stage = "mutation"
    progress.effect = "not_applied"
    await AlertRepository(session).set_status(alert, AlertStatus.PAUSED, require_editable=True)
    action.applied_at = datetime.now(UTC)
    action.applied_version = alert.decision_version
    action.update_id, action.callback_id = progress.update_id, progress.callback_id
    progress.stage = "commit"
    progress.effect = "unknown"
    await session.commit()
    progress.effect = "committed"
    progress.result = "applied"
    progress.current_status = alert.status.value
    return "applied"


async def issue_pause_button(alert: Alert, session: AsyncSession) -> str | None:
    """Persiste l'autorisation avant sa présentation, sans demander une pause."""
    from app.db.repositories.pause_action_repo import PauseActionRepository

    action = await PauseActionRepository(session).issue(alert)
    if action is None:
        return None
    await session.commit()
    return f"al_pause:p_{action.id.hex}"
