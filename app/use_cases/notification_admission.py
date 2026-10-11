"""Admission persistée, appelée exclusivement sous le gate commun de l'annonce."""

import time
import uuid
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import is_telegram_user_allowed
from app.db.models.alert import Alert
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.domain.enums import AlertStatus
from app.domain.enums import NotificationState as State
from app.services.telegram_service import DeliveryResult

MAX_CYCLES = 3
MAX_AGE = timedelta(minutes=30)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def notification_row(db: AsyncSession, alert_id: uuid.UUID,
                           listing_id: uuid.UUID) -> ListingMatch | None:
    return await db.scalar(select(ListingMatch).where(
        ListingMatch.alert_id == alert_id, ListingMatch.listing_id == listing_id,
    ).execution_options(populate_existing=True))


async def complete_stored_receipt(db: AsyncSession, row: ListingMatch) -> bool:
    """Un reçu conservé ne permet que le marquage SQL ; aucune hypothèse sur les champs absents."""
    if not (
        type(row.telegram_message_id) is int and row.telegram_message_id > 0
        and type(row.telegram_chat_id) is int and row.telegram_chat_id != 0
        and type(row.telegram_bot_id) is int and row.telegram_bot_id > 0
        and row.attempt_id is not None
    ):
        return False
    from app.db.repositories.listing_repo import ListingRepository
    marked = await ListingRepository(db).mark_notified([(row.alert_id, row.listing_id)])
    await db.commit()
    return marked


async def reserve(db: AsyncSession, listing: Listing, alert: Alert,
                  bot: Bot, attempt_id: uuid.UUID, deadline: float | None = None) -> bool:
    row = await notification_row(db, alert.id, listing.id)
    if row is None or row.notified_at is not None:
        return False
    if await complete_stored_receipt(db, row):
        return False
    now = datetime.now(UTC)
    if row.notification_state == State.SENDING:
        # Sous le gate, aucun autre propriétaire de CETTE annonce ne peut encore envoyer.
        row.notification_state, row.notification_reason = State.UNKNOWN, "orphaned_sending"
        await db.commit()
        return False
    if row.notification_state not in {State.READY, State.DEFERRED}:
        return False
    if (isinstance(row.attempt_count, bool) or not isinstance(row.attempt_count, int)
            or row.attempt_count < 0):
        row.notification_state, row.notification_reason = State.UNKNOWN, "missing_attempt_evidence"
    elif now >= utc(listing.received_at) + MAX_AGE or row.attempt_count >= MAX_CYCLES:
        row.notification_state, row.notification_reason = State.ABANDONED, "budget_or_age"
    elif (alert.status != AlertStatus.ACTIVE or alert.deleted_at is not None or alert.preparation
          or not alert.user or not alert.user.is_allowed
          or not is_telegram_user_allowed(alert.user.telegram_user_id)):
        row.notification_state, row.notification_reason = State.DEFERRED, "inadmissible_recipient"
        row.next_attempt_at = now + timedelta(minutes=1)
    elif row.telegram_chat_id is not None and row.telegram_chat_id != alert.user.telegram_user_id:
        row.notification_state, row.notification_reason = State.ABANDONED, "destination_changed"
    elif row.next_attempt_at is not None and utc(row.next_attempt_at) > now:
        return False
    else:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        claimed = await db.scalar(update(ListingMatch).where(
            ListingMatch.id == row.id, ListingMatch.notified_at.is_(None),
            ListingMatch.notification_state.in_([State.READY, State.DEFERRED]),
            ListingMatch.attempt_count == row.attempt_count,
        ).values(notification_state=State.SENDING, attempt_id=attempt_id,
                 attempt_count=row.attempt_count + 1, attempt_started_at=now,
                 telegram_chat_id=alert.user.telegram_user_id, telegram_bot_id=bot.id,
                 next_attempt_at=None).returning(ListingMatch.id))
        await db.commit()  # Sans retour réussi, l'appelant n'envoie rien.
        return claimed is not None
    await db.commit()
    return False


async def record_result(db: AsyncSession, listing: Listing, alert_id: uuid.UUID,
                        attempt_id: uuid.UUID, delivery: DeliveryResult) -> None:
    row = await notification_row(db, alert_id, listing.id)
    if row is None or row.notified_at is not None or row.attempt_id != attempt_id:
        return
    if delivery:
        assert delivery.receipt is not None
        row.telegram_message_id = delivery.receipt.message_id
        # Le marquage et le reçu sont commités ensemble par _persist_confirmed_delivery.
        return
    reasons = {
        "local_validation", "api_rejected", "client_result_unknown", "photo_content_rejected",
        "unusable_receipt", "invalid_sender_result", "wrong_destination",
    }
    row.notification_reason = (
        delivery.reason if delivery.reason in reasons else "unclassified_result"
    )
    if delivery.outcome == "unknown":
        row.notification_state = State.UNKNOWN
    elif delivery.outcome == "rejected" and delivery.retry_after is not None:
        delay = max((60, 300)[min((row.attempt_count or 1) - 1, 1)], delivery.retry_after)
        if delay > MAX_AGE.total_seconds():
            row.notification_state = State.ABANDONED
            row.notification_reason = "retry_exceeds_budget"
            await db.commit()
            return
        due = datetime.now(UTC) + timedelta(seconds=delay)
        if ((row.attempt_count or MAX_CYCLES) >= MAX_CYCLES
                or due >= utc(listing.received_at) + MAX_AGE):
            row.notification_state = State.ABANDONED
            row.notification_reason = "retry_exceeds_budget"
        else:
            row.notification_state, row.next_attempt_at = State.READY, due
    else:
        row.notification_state = State.ABANDONED
    await db.commit()
