"""Cas d'usage : Ingestion, matching déterministe et orchestration d'alerte."""

import asyncio
import logging
import uuid
from dataclasses import dataclass, replace
from typing import Any, Protocol

from aiogram import Bot
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.bot_instance import bot as default_bot
from app.core import alert_latency as latency
from app.core.constants import LEBONCOIN_SOURCE_NAME, CategorySlug
from app.core.tasks import safe_create_task
from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.models.bot_event import BotEvent
from app.db.models.listing import Listing
from app.db.repositories.alert_repo import AlertRepository, BibliographicSnapshot
from app.db.repositories.book_repo import BookRepository, resolve_loaded_books
from app.db.repositories.listing_repo import ListingRepository
from app.domain.enums import NotificationState
from app.domain.rules.catalogue_search import is_uncatalogued
from app.domain.rules.listing_freshness import listing_is_new_for_alert
from app.domain.rules.listing_match import evaluate_listing_against_alert
from app.schemas.listing import (
    ListingIngestPayload,
    ListingIngestResponse,
    ListingIngestWarning,
    ListingMatchItem,
)
from app.services.book_market_pricing import (
    recalculate_invalidated_series,
)
from app.services.market_health import classify_sent_cote_state, record_sent_cote_state
from app.services.market_price_service import (
    COTE_NOT_READY_LINE,
    NO_CATALOGUE_LINE,
    PENDING_COTE_LINE,
    SEARCHING_BOOK_LINE,
    MarketPriceService,
    cote_absence_line,
)
from app.services.telegram_service import DeliveryResult, send_listing_alert, usable_receipt
from app.use_cases.link_bd_catalog import resolve_bd_catalog_book
from app.use_cases.listing_ingestion_gate import listing_ingestion_gate
from app.use_cases.notification_admission import record_result, reserve
from app.utils.isbn import normalize_listing_isbn

logger = logging.getLogger("radar.use_cases.ingest_listing")


def comparison_line_from_loaded_books(
    listing: Listing,
    alert: Any,
) -> tuple[str | None, bool, Book | None]:
    """Cote synchrone depuis les livres déjà chargés. False si le catalogue est vide."""
    links = getattr(alert, "alert_books", None)
    books = []
    if isinstance(links, list):
        books = [link.book for link in links if getattr(link, "book", None) is not None]
    expected_isbn = getattr(alert, "isbn", None)
    if isinstance(expected_isbn, str) and expected_isbn:
        expected = normalize_listing_isbn(expected_isbn)
        books = [book for book in books if expected
                 and normalize_listing_isbn(book.isbn13) == expected]
    if not books:
        return None, False, None
    book, series, tome_result = resolve_loaded_books(
        alert_name=alert.name,
        listing_title=listing.title,
        listing_description=listing.description,
        books=books,
    )
    line = MarketPriceService.get_comparison_summary(
        listing=listing,
        book=book,
        books_in_series=series,
        tome_result=tome_result,
    )
    # Le livre est rattrapé en arrière-plan ; le message est un instantané.
    absence = line or cote_absence_line(book)
    if absence == PENDING_COTE_LINE:
        absence = COTE_NOT_READY_LINE
    return absence, True, book


@latency.observe_background("bibliography")
async def _link_bd_catalog_background(
    *,
    alert_id: uuid.UUID,
    alert_name: str,
    alert_isbn: str | None,
    listing_title: str,
    listing_description: str | None,
    listing_isbn13: str | None,
    alert_preparation: dict[str, Any] | None = None,
    alert_snapshot: BibliographicSnapshot | None = None,
) -> None:
    """Lie le catalogue BD après l'alerte, dans une session dédiée."""
    from app.db.session import AsyncSessionLocal

    seller_isbn = normalize_listing_isbn(listing_isbn13)
    if seller_isbn and not alert_isbn:
        from app.use_cases.listing_book_confirmation import confirm_listing_book
        try:
            await confirm_listing_book(
                alert_id=alert_id, alert_name=alert_name, alert_isbn=alert_isbn,
                expected_preparation=alert_preparation, listing_isbn=seller_isbn,
                listing_title=listing_title, listing_description=listing_description,
                expected_snapshot=alert_snapshot,
            )
        except Exception:
            logger.warning("Confirmation ISBN vendeur interrompue.",
                           extra={"event": "listing_isbn_confirmation_failed"}, exc_info=True)
        return

    try:
        async with AsyncSessionLocal() as session:
            repo = AlertRepository(session)
            current = await repo.get_by_id(alert_id)
            if current is None or (current.name, current.isbn) != (alert_name, alert_isbn):
                return
            snapshot = alert_snapshot or BibliographicSnapshot.capture(current)
            if not snapshot.matches(current):
                return
            if alert_isbn is not None:
                current = await repo.lock_bibliographic_result(alert_id, snapshot)
                if current is None:
                    return
            await resolve_bd_catalog_book(
                session,
                alert_id=alert_id,
                alert_name=alert_name,
                alert_isbn=alert_isbn,
                listing_title=listing_title,
                listing_description=listing_description,
                alert_snapshot=snapshot,
            )
            await recalculate_invalidated_series(session)
            await session.commit()
    except Exception as exc:
        logger.debug("Liaison catalogue BD différée ignorée pour %s : %s", alert_id, exc)
        return



def _alert_has_telegram_chat(alert: Any) -> bool:
    """Vrai si l'alerte a un destinataire Telegram à qui envoyer."""
    user = getattr(alert, "user", None)
    if user is None:
        return False
    return bool(getattr(user, "telegram_user_id", None))


class ListingAlertSender(Protocol):
    """Émetteur d'une notification avec son résultat de livraison explicite."""

    async def __call__(
        self,
        *,
        bot: Bot,
        chat_id: int,
        listing: Listing,
        market_comparison_line: str | None,
        alert_keywords: list[str] | None,
        alert: Alert,
        attempt_id: uuid.UUID,
    ) -> DeliveryResult: ...


PendingSend = tuple[Any, Any, str | None, bool, Book | None]


@dataclass
class _NotificationContext:
    """Identifiants retenus avant tout rollback ; les objets restent propres à cette session."""

    listing: Listing
    listing_id: uuid.UUID
    alert_ids: list[uuid.UUID]
    book_ids: list[uuid.UUID | None]
    live_alerts: set[uuid.UUID]
    live_books: set[uuid.UUID]


class ProcessListingUseCase:
    """Orchestre la déduplication, le matching, la comparaison de prix et les alertes."""

    def __init__(
        self,
        db: AsyncSession,
        bot: Bot | None = None,
        *,
        sender: ListingAlertSender | None = None,
    ) -> None:
        self.db = db
        self.bot = bot or default_bot
        self._sender = send_listing_alert if sender is None else sender
        self.listing_repo = ListingRepository(db)

    async def execute(
        self,
        payload: ListingIngestPayload,
        raw_payload: dict[str, Any],
    ) -> ListingIngestResponse:
        """Sérialise l'annonce avant toute décision et jusqu'aux commits de notification."""
        timing = latency.current() or latency.AlertTiming(ingestion_id=uuid.uuid4())
        timing.entered_at = latency.clock()
        outcome = "error"
        acquired = False
        waiting_at = latency.clock()
        with latency.timing_context(timing):
            if timing.discovered_at is not None:
                latency.emit(
                    timing,
                    "discovery_to_entry",
                    timing.discovered_at,
                    timing.entered_at,
                    result="entered",
                )
            try:
                async with listing_ingestion_gate.protect(payload.source_name, payload.external_id):
                    acquired = True
                    timing.acquired_at = latency.clock()
                    latency.emit(
                        timing, "guard_wait", waiting_at, timing.acquired_at, result="acquired"
                    )
                    try:
                        response = await self._execute_serialized(payload, raw_payload)
                        outcome = (
                            "attempted"
                            if timing.attempts
                            else "duplicate_no_send"
                            if response.is_duplicate
                            else "no_match"
                            if not response.matches
                            else "no_eligible_recipient"
                        )
                        return response
                    except BaseException:
                        await self._rollback_before_release()
                        raise
            except BaseException as exc:
                outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                if not acquired:
                    latency.emit(
                        timing,
                        "guard_wait",
                        waiting_at,
                        latency.clock(),
                        result=outcome,
                        error_type=type(exc).__name__,
                    )
                raise
            finally:
                timing.alert_id = timing.attempt_id = None
                latency.emit(
                    timing,
                    "ingestion_terminal",
                    timing.entered_at,
                    latency.clock(),
                    result=outcome,
                    attempts=timing.attempts,
                )

    async def _rollback_before_release(self) -> None:
        """Termine le rollback sous protection, même si l'appelant est annulé pendant celui-ci."""
        rollback = safe_create_task(self.db.rollback(), name="ingest_rollback", cleanup=True)
        cancelled = False
        while not rollback.done():
            try:
                await asyncio.shield(rollback)
            except asyncio.CancelledError:
                cancelled = True
        rollback.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _execute_serialized(
        self,
        payload: ListingIngestPayload,
        raw_payload: dict[str, Any],
        resume_ids: set[uuid.UUID] | None = None,
        admission_deadline: float | None = None,
    ) -> ListingIngestResponse:
        """Traite l'annonce avec garantie de latence minimale sur l'envoi d'alerte."""
        existing_listing = await self.listing_repo.get_by_source_and_external_id(
            source_name=payload.source_name,
            external_id=payload.external_id,
        )

        notified_ids: set[uuid.UUID] = set()
        pending_ids: set[uuid.UUID] = set()
        if existing_listing:
            listing = existing_listing
            is_duplicate = True
            notified_ids, pending_ids = await self.listing_repo.match_notification_state(listing.id)
        else:
            listing = await self.listing_repo.create_from_payload(
                payload,
                raw_dict=raw_payload,
                apply_default_fees=payload.source_name != LEBONCOIN_SOURCE_NAME,
            )
            is_duplicate = False

        timing = latency.current()
        if timing is not None:
            timing.listing_id = listing.id
        effective_isbn = normalize_listing_isbn(listing.isbn13)
        warnings: list[ListingIngestWarning] = []
        if payload.isbn_invalid_ignored or (listing.isbn13 is not None and effective_isbn is None):
            warnings.append("listing_isbn_invalid_ignored")
            logger.warning("ISBN vendeur inutilisable écarté ; annonce conservée.",
                           extra={"event": "listing_isbn_invalid_ignored"})

        active_alerts = await self.listing_repo.get_active_alerts_by_category(payload.category_slug)
        pending_sends: list[PendingSend] = []
        for alert in active_alerts:
            if not listing_is_new_for_alert(alert, listing):
                if resume_ids is not None and alert.id in resume_ids:
                    from app.use_cases.notification_admission import notification_row
                    row = await notification_row(self.db, alert.id, listing.id)
                    if row is not None and row.notification_state in {
                        NotificationState.READY, NotificationState.DEFERRED,
                    }:
                        row.notification_state = NotificationState.ABANDONED
                        row.notification_reason = "initial_catalogue"
                continue
            if resume_ids is not None and alert.id not in resume_ids:
                continue
            match_result = evaluate_listing_against_alert(
                alert=alert,
                listing_category_slug=listing.category_slug,
                listing_normalized_title=listing.normalized_title,
                listing_normalized_description=listing.description,
                listing_condition=listing.condition,
                listing_item_price_cents=listing.item_price_cents,
                listing_isbn13=effective_isbn,
                listing_raw_url=listing.url,
            )
            if not match_result.is_match or alert.id in notified_ids:
                if resume_ids is not None and not match_result.is_match:
                    from app.use_cases.notification_admission import notification_row
                    row = await notification_row(self.db, alert.id, listing.id)
                    if row is not None and row.notification_state in {
                        NotificationState.READY, NotificationState.DEFERRED,
                    }:
                        row.notification_state = NotificationState.ABANDONED
                        row.notification_reason = "matching_changed"
                continue
            market_line = None
            catalog_ready = False
            resolved_book: Book | None = None
            if listing.category_slug == CategorySlug.BD_COMICS.value:
                market_line, catalog_ready, resolved_book = comparison_line_from_loaded_books(
                    listing, alert
                )
                if not catalog_ready:
                    (
                        market_line,
                        catalog_ready,
                        resolved_book,
                    ) = await self._comparison_from_catalog_isbn(listing, alert)
                # Aucune alerte BD ne part sans bloc de cote.
                if market_line is None:
                    if catalog_ready:
                        market_line = COTE_NOT_READY_LINE
                    elif is_uncatalogued(alert):
                        # Recherche déjà répondue : pas de « recherche en cours ».
                        market_line = NO_CATALOGUE_LINE
                    else:
                        market_line = SEARCHING_BOOK_LINE
            pending_sends.append((alert, match_result, market_line, catalog_ready, resolved_book))

        if is_duplicate and not pending_sends and not pending_ids:
            return ListingIngestResponse(
                listing_id=listing.id,
                is_duplicate=True,
                estimated_total_cents=listing.estimated_total_cents,
                matches=[],
                warnings=warnings,
            )

        for alert, match_result, _line, _ready, _book in pending_sends:
            if resume_ids is not None:
                continue  # La reprise n'invente jamais une association disparue.
            if alert.id in pending_ids:
                continue
            if not _alert_has_telegram_chat(alert):
                continue
            await self.listing_repo.create_match(
                alert_id=alert.id,
                listing_id=listing.id,
                keyword_match_score=match_result.keyword_match_score,
                condition_matches=match_result.condition_matches,
                price_matches=match_result.price_matches,
                notified=False,
                known_unsent=not is_duplicate,
            )

        actionable_matches_count = sum(
            1 for alert, _, _, _, _ in pending_sends if _alert_has_telegram_chat(alert)
        )
        event = BotEvent(
            event_type="listing_ingested",
            listing_id=listing.id,
            metadata_={
                "source_name": payload.source_name,
                "external_id": payload.external_id,
                "matches_count": actionable_matches_count,
                "is_duplicate": is_duplicate,
            },
        )
        self.db.add(event)
        await self.db.commit()

        if timing is not None:
            timing.recipients_ready_at = latency.clock()
        attempt_ids: dict[uuid.UUID, uuid.UUID] = {}
        alert_ids = [alert.id for alert, *_ in pending_sends]
        book_ids = [book.id if book is not None else None for *_, book in pending_sends]
        context = _NotificationContext(
            listing,
            listing.id,
            alert_ids,
            book_ids,
            set(alert_ids),
            {value for value in book_ids if value is not None},
        )
        committed_deliveries: list[tuple[uuid.UUID, uuid.UUID, DeliveryResult]] = []
        match_items: list[ListingMatchItem] = []
        needs_retry = False
        catalog_tasks: list[dict[str, Any]] = []
        for index, pending in enumerate(pending_sends):
            if admission_deadline is not None:
                import time
                if time.monotonic() >= admission_deadline:
                    break
            alert, _match_result, market_line, catalog_ready, book = pending
            alert_id = context.alert_ids[index]
            if alert_id not in context.live_alerts:
                continue
            notification_sent = False
            if _alert_has_telegram_chat(alert):
                alert_kws = alert.include_keywords or ([alert.name] if alert.name else None)
                attempt_id = uuid.uuid4()
                if not await reserve(
                    self.db, listing, alert, self.bot, attempt_id, deadline=admission_deadline,
                ):
                    continue
                attempt_ids[alert_id] = attempt_id
                if timing is not None:
                    timing.alert_id, timing.attempt_id = alert_id, attempt_id
                    timing.ack_at = None
                    timing.attempts += 1
                    latency.emit(timing, "recipient_wait", timing.recipients_ready_at,
                                 latency.clock(), result="ready")
                delivery = await self._sender(
                    bot=self.bot,
                    chat_id=alert.user.telegram_user_id,
                    listing=listing,
                    market_comparison_line=market_line,
                    alert_keywords=alert_kws,
                    alert=alert,
                    attempt_id=attempt_id,
                )
                if (
                    not isinstance(delivery, DeliveryResult)
                    or type(delivery.outcome) is not str or type(delivery.transport) is not str
                    or delivery.outcome not in {"not_sent", "acknowledged", "rejected", "unknown"}
                    or delivery.transport not in {"none", "photo", "text"}
                    or (delivery.outcome == "not_sent" and delivery.transport != "none")
                ):
                    delivery = DeliveryResult("unknown", "none", reason="invalid_sender_result")
                    logger.error(
                        "Résultat d'envoi inexploitable.",
                        extra=delivery.diagnostics(
                            alert_id=alert_id,
                            listing_id=context.listing_id,
                            attempt_id=attempt_id,
                            persistence_stage="not_started",
                        ),
                    )
                notification_sent = bool(delivery) and usable_receipt(
                    delivery.receipt, alert.user.telegram_user_id
                )
                if not notification_sent:
                    if delivery.outcome == "acknowledged":
                        delivery = DeliveryResult("unknown", "none", reason="wrong_destination")
                    await record_result(self.db, listing, alert_id, attempt_id, delivery)
                if notification_sent:
                    if await self._persist_confirmed_delivery(
                        delivery, alert_id, attempt_id, context
                    ):
                        committed_deliveries.append((alert_id, attempt_id, delivery))
                else:
                    from app.use_cases.notification_admission import notification_row
                    current_notification = await notification_row(self.db, alert_id, listing.id)
                    needs_retry = needs_retry or (
                        current_notification is not None
                        and current_notification.notification_state == NotificationState.READY
                    )
            if listing.category_slug == CategorySlug.BD_COMICS.value:
                if notification_sent:
                    # Mesure après l'envoi, sur la ligne réellement reçue.
                    safe_create_task(
                        record_sent_cote_state(classify_sent_cote_state(market_line)),
                        name=f"cote_state_{alert.id}",
                    )
                if not catalog_ready:
                    catalog_tasks.append(
                        {
                            "alert_id": alert.id,
                            "alert_name": alert.name,
                            "alert_isbn": getattr(alert, "isbn", None),
                            "alert_snapshot": BibliographicSnapshot.capture(alert),
                            "listing_title": listing.title,
                            "listing_description": listing.description,
                            "listing_isbn13": effective_isbn,
                            "alert_preparation": getattr(alert, "preparation", None)
                            if isinstance(getattr(alert, "preparation", None), dict)
                            else None,
                        }
                    )
            match_items.append(
                ListingMatchItem(
                    alert_id=alert.id,
                    alert_name=alert.name,
                    notification_sent=notification_sent,
                )
            )

        for arguments in catalog_tasks:
            task_timing = (
                replace(
                    timing,
                    alert_id=arguments["alert_id"],
                    attempt_id=attempt_ids.get(arguments["alert_id"]),
                )
                if timing
                else None
            )
            with latency.timing_context(task_timing):
                safe_create_task(
                    _link_bd_catalog_background(**arguments),
                    name=f"bd_catalog_{arguments['alert_id']}",
                )

        final_started = None
        final_called = False
        final_result = "committed"
        try:
            recalculation_started = latency.clock()
            recalculation_result = "returned"
            try:
                await recalculate_invalidated_series(self.db)
            except BaseException as exc:
                recalculation_result = (
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                )
                raise
            finally:
                if timing is not None:
                    latency.emit(
                        replace(timing, alert_id=None, attempt_id=None),
                        "post_send_recalculation",
                        recalculation_started,
                        latency.clock(),
                        result=recalculation_result,
                    )
            final_started = latency.clock()
            final_called = True
            await self.db.commit()
        except BaseException as exc:
            final_result = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            for alert_id, attempt_id, delivery in committed_deliveries:
                latency.safe_log(
                    logger,
                    logging.WARNING,
                    "Échec final après marquage durable, sans remise en attente.",
                    delivery.diagnostics(
                        alert_id=alert_id,
                        listing_id=context.listing_id,
                        attempt_id=attempt_id,
                        persistence_stage="final_commit_failed",
                        persistence_result="notified",
                        persistence_error=type(exc).__name__,
                    ),
                )
            raise
        finally:
            if timing is not None and final_called:
                with latency.timing_context(replace(timing, alert_id=None, attempt_id=None)):
                    latency.emit(latency.current() or timing, "final_commit", final_started,
                                 latency.clock(), result=final_result)

        return ListingIngestResponse(
            listing_id=listing.id,
            is_duplicate=is_duplicate,
            estimated_total_cents=listing.estimated_total_cents,
            matches=match_items,
            needs_retry=needs_retry,
            warnings=warnings,
        )

    async def _persist_confirmed_delivery(
        self,
        delivery: DeliveryResult,
        alert_id: uuid.UUID,
        attempt_id: uuid.UUID,
        context: _NotificationContext,
    ) -> bool:
        """Un nouvel essai SQL au plus, jamais de renvoi Telegram ni de reprise après annulation."""
        stage = "marking"
        sql_attempt = 1
        persistence_result = "unknown"
        timing = latency.current()

        def diagnostic(result: str, error: BaseException | None = None) -> None:
            latency.safe_log(
                logger, logging.WARNING if error else logging.INFO,
                "État de persistance de la notification.",
                delivery.diagnostics(
                    alert_id=alert_id,
                    listing_id=context.listing_id,
                    attempt_id=attempt_id,
                    persistence_stage=stage,
                    persistence_result=result,
                    sql_attempt=sql_attempt,
                    persistence_error=type(error).__name__ if error else None,
                ),
            )

        try:
            for sql_attempt in (1, 2):
                stage = "marking" if sql_attempt == 1 else "retry_marking"
                try:
                    await record_result(self.db, context.listing, alert_id, attempt_id, delivery)
                    diagnostic("pending")
                    marked = await self.listing_repo.mark_notified(
                        [(alert_id, context.listing_id)]
                    )
                    stage = "committing"
                    await self.db.commit()
                    stage = "committed" if marked else "match_missing"
                    persistence_result = "notified" if marked else "unknown"
                    diagnostic(persistence_result)
                    return bool(marked)
                except (SQLAlchemyError, TimeoutError, OSError) as exc:
                    diagnostic("unknown", exc)
                    stage = "rollback"
                    await self._rollback_before_release()
                    diagnostic("unknown")
                    if sql_attempt == 2:
                        raise
                    stage = "reread"
                    notified, pending = await self.listing_repo.match_notification_state(
                        context.listing_id
                    )
                    (
                        context.live_alerts,
                        context.live_books,
                    ) = await self.listing_repo.refresh_notification_context(
                        context.listing,
                        context.alert_ids,
                        [value for value in context.book_ids if value is not None],
                    )
                    if alert_id in notified:
                        stage = "reread_committed"
                        persistence_result = "notified"
                        diagnostic("notified")
                        return True
                    if alert_id not in pending:
                        stage = "match_missing"
                        diagnostic("unknown", exc)
                        raise
            raise AssertionError("Les deux tentatives de marquage doivent terminer ou échouer.")
        except asyncio.CancelledError as exc:
            persistence_result = "cancelled"
            diagnostic("unknown", exc)
            raise
        except Exception as exc:
            diagnostic("unknown", exc)
            raise
        finally:
            if timing is not None:
                latency.emit(timing, "ack_to_persistence", timing.ack_at, latency.clock(),
                             result=persistence_result, sql_attempt=sql_attempt,
                             persistence_stage=stage,
                             message_id=delivery.receipt.message_id if delivery.receipt else None)

    async def _comparison_from_catalog_isbn(
        self,
        listing: Listing,
        alert: Any,
    ) -> tuple[str | None, bool, Book | None]:
        """Une lecture ISBN indexée si le livre est déjà au catalogue. Pas de BNF."""
        isbn = getattr(alert, "isbn", None)
        if not isbn:
            return None, False, None
        snapshot = BibliographicSnapshot.capture(alert)
        book_repo = BookRepository(self.db)
        book = await book_repo.get_by_isbn13(isbn)
        if book is None:
            return None, False, None
        if await AlertRepository(self.db).lock_bibliographic_result(alert.id, snapshot) is None:
            return None, False, None
        await book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)
        selected, books, tome_result = resolve_loaded_books(
            alert_name=alert.name,
            listing_title=listing.title,
            listing_description=listing.description,
            books=[book],
        )
        line = MarketPriceService.get_comparison_summary(
            listing=listing,
            book=selected or (book if not tome_result.is_lot else None),
            books_in_series=books,
            tome_result=tome_result,
        )
        return line or cote_absence_line(book), True, book
