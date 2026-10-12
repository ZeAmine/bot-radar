"""Verrous et preuves propres à la pause ; aucune présentation ou commit implicite."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.alert import Alert
from app.db.models.pause_action import PauseAction
from app.db.repositories.alert_repo import AlertRepository
from app.domain.entities.alert_preparation import preparation_request
from app.domain.enums import AlertStatus


def utc(value: datetime) -> datetime:
    """SQLite restitue les dates naïves ; les écritures de ce contrat sont UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class PauseActionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def issue(self, alert: Alert, *, now: datetime | None = None) -> PauseAction | None:
        request = preparation_request(alert)
        intent = (
            "suspend_preparation"
            if request is not None
            and request.activation_requested
            and request.state == "pending"
            and alert.status == AlertStatus.PAUSED
            else "pause"
            if alert.status == AlertStatus.ACTIVE and request is None
            else None
        )
        if intent is None or alert.deleted_at is not None:
            return None
        at = now or datetime.now(UTC)
        action = PauseAction(
            user_id=alert.user_id,
            alert_id=alert.id,
            expected_version=alert.decision_version,
            intent=intent,
            issued_at=at,
            expires_at=at + timedelta(hours=24),
        )
        self.session.add(action)
        await self.session.flush()
        return action

    async def acquire(
        self, token: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[PauseAction | None, Alert | None]:
        # Lecture d'adresse seule. Ordre constant : alerte, puis action ; la décision
        # repose exclusivement sur les relectures verrouillées, pas sur cette lecture.
        address = await self.session.scalar(
            select(PauseAction.alert_id).where(
                PauseAction.id == token,
                PauseAction.user_id == user_id,
            )
        )
        if address is None:
            return None, None
        repo = AlertRepository(self.session)
        alert = await repo.get_by_id(address, user_id=user_id)
        if alert is not None and not await repo.lock_for_edit(alert, include_deleted=True):
            alert = None
        if alert is not None and alert.user_id != user_id:
            alert = None
        action = await self.session.scalar(
            select(PauseAction)
            .where(
                PauseAction.id == token,
                PauseAction.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return action, alert

    async def purge(self, *, now: datetime | None = None) -> None:
        await self.session.execute(
            delete(PauseAction).where(
                PauseAction.issued_at <= (now or datetime.now(UTC)) - timedelta(hours=48),
            )
        )
