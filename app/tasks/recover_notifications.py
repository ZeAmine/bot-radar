"""Reprise SQL bornée, sans recollecte ni réveil des résultats inconnus."""

import logging
import time
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.db.models.alert import Alert
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.db.session import AsyncSessionLocal
from app.domain.enums import AlertStatus, NotificationState
from app.schemas.listing import ListingIngestPayload
from app.use_cases.listing_ingestion_gate import IngestionBusyError, listing_ingestion_gate
from app.use_cases.notification_admission import MAX_AGE, complete_stored_receipt, utc


def get_bot() -> Bot:
    from app.bot.bot_instance import bot
    return bot


async def recover_notifications() -> None:
    """Le scheduler reçoit un diagnostic borné, jamais un corps d'erreur externe."""
    try:
        await _recover_notifications()
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Reprise de notifications interrompue (%s).", type(exc).__name__,
            extra={"event": "notification_recovery_failed", "error_type": type(exc).__name__},
        )


async def _recover_notifications() -> None:
    """Dix destinataires au plus ; vingt secondes bornent les nouvelles admissions."""
    from app.use_cases.ingest_listing import ProcessListingUseCase

    bot = get_bot()
    deadline = time.monotonic() + 20
    async with AsyncSessionLocal() as reader:
        ids = list((await reader.scalars(select(ListingMatch.id).where(
            ListingMatch.notified_at.is_(None),
            or_(ListingMatch.notification_state.in_([
                NotificationState.READY, NotificationState.DEFERRED, NotificationState.SENDING,
            ]),
                (ListingMatch.telegram_message_id > 0)
                & (ListingMatch.telegram_chat_id != 0)
                & (ListingMatch.telegram_bot_id > 0)
                & ListingMatch.attempt_id.is_not(None)),
            or_(ListingMatch.next_attempt_at.is_(None),
                ListingMatch.next_attempt_at <= datetime.now(UTC)),
        ).order_by(ListingMatch.created_at, ListingMatch.id).limit(10))).all())
    for match_id in ids:
        if time.monotonic() >= deadline:
            break
        async with AsyncSessionLocal() as session:
            row = await session.get(ListingMatch, match_id)
            listing = await session.get(Listing, row.listing_id) if row else None
            if row is None or listing is None:
                continue
            source, external_id = listing.source_name, listing.external_id
            await session.rollback()
            try:
                async with listing_ingestion_gate.protect(source, external_id):
                    use = ProcessListingUseCase(session, bot)
                    try:
                        if time.monotonic() >= deadline:
                            break
                        row = await session.get(ListingMatch, match_id, populate_existing=True)
                        if row is None or row.notified_at is not None:
                            continue
                        listing = await session.get(Listing, row.listing_id, populate_existing=True)
                        if listing is None:
                            continue
                        if (listing.source_name, listing.external_id) != (source, external_id):
                            continue
                        if await complete_stored_receipt(session, row):
                            continue
                        # Le propriétaire éventuel a terminé avant cette relecture sous gate.
                        if row.notification_state == NotificationState.SENDING:
                            row.notification_state = NotificationState.UNKNOWN
                            row.notification_reason = "orphaned_sending"
                            await session.commit()
                            continue
                        if row.notification_state not in {
                            NotificationState.READY, NotificationState.DEFERRED,
                        }:
                            continue
                        if (isinstance(row.attempt_count, bool)
                                or not isinstance(row.attempt_count, int)
                                or row.attempt_count < 0):
                            row.notification_state = NotificationState.UNKNOWN
                            row.notification_reason = "missing_attempt_evidence"
                            await session.commit()
                            continue
                        if datetime.now(UTC) >= utc(listing.received_at) + MAX_AGE:
                            row.notification_state = NotificationState.ABANDONED
                            row.notification_reason = "age_limit"
                            await session.commit()
                            continue
                        alert = await session.scalar(select(Alert).where(Alert.id == row.alert_id)
                                                     .options(selectinload(Alert.user)))
                        if alert is None:
                            continue
                        if alert.status != AlertStatus.ACTIVE or alert.preparation:
                            row.notification_state = NotificationState.DEFERRED
                            row.notification_reason = "inactive_alert"
                            row.next_attempt_at = datetime.now(UTC) + timedelta(minutes=1)
                            await session.commit()
                            continue
                        try:
                            payload = ListingIngestPayload.model_validate({
                                name: getattr(listing, name)
                                for name in ListingIngestPayload.model_fields
                                if hasattr(listing, name)
                            })
                        except ValidationError:
                            row.notification_state = NotificationState.ABANDONED
                            row.notification_reason = "invalid_persisted_listing"
                            await session.commit()
                            continue
                        await use._execute_serialized(
                            payload, {}, resume_ids={row.alert_id}, admission_deadline=deadline,
                        )
                    except BaseException:
                        await use._rollback_before_release()
                        raise
            except IngestionBusyError:
                continue  # Aucun cycle réservé ; pas de file d'admission supplémentaire.
