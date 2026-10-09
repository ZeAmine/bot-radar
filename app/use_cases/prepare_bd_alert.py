"""Préparation BD durable, sans dépendance au FSM ou à un ancien message Telegram."""

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from app.core.tasks import safe_create_task
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.book_repo import BookRepository
from app.db.session import AsyncSessionLocal
from app.domain.entities.alert_preparation import (
    PreparationError,
    PreparationErrorCode,
    preparation_request,
)
from app.domain.enums import AlertStatus
from app.services.book_market_pricing import price_book_now
from app.utils.text import normalize_text

logger = logging.getLogger(__name__)
Outcome = Literal["completed", "superseded", "stopped", "blocked"]


class PreparationRunner:
    """Un travail par alerte, deux travaux réseau au plus, un seul passage de reprise."""

    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, asyncio.Task[Outcome]] = {}
        self.slots = asyncio.Semaphore(2)
        self.pass_lock = asyncio.Lock()
        self.deferred_blocks: dict[uuid.UUID, tuple[str, PreparationErrorCode]] = {}

    async def prepare(self, alert_id: uuid.UUID) -> bool:
        """Rejoint le travail courant ; une version dépassée provoque une lecture nouvelle."""
        while True:
            task = self.tasks.get(alert_id)
            if task is None:
                task = safe_create_task(self._run(alert_id), name=f"prepare_bd_{alert_id}")
                self.tasks[alert_id] = task

                def release(done: asyncio.Task[Outcome]) -> None:
                    if self.tasks.get(alert_id) is done:
                        del self.tasks[alert_id]

                task.add_done_callback(release)
            outcome = await asyncio.shield(task)
            if self.tasks.get(alert_id) is task and task.done():
                del self.tasks[alert_id]
            # Une action peut avoir changé le jeton entre la fin du travail et
            # le réveil d'un appelant. Son résultat n'est pas celui de cette version.
            async with AsyncSessionLocal() as session:
                current = await AlertRepository(session).get_by_id(alert_id, refresh=True)
                if current is None or current.deleted_at is not None:
                    return False
                request = preparation_request(current)
                if request is not None:
                    deferred = self.deferred_blocks.get(alert_id)
                    if deferred is not None and deferred[0] == request.token:
                        return False
                    if (
                        request.state == "pending"
                        and request.activation_requested
                        and current.status == AlertStatus.PAUSED
                    ):
                        continue
                return outcome == "completed" and current.status == AlertStatus.ACTIVE

    async def recover(self) -> None:
        """Passe bornée et équitable ; les travaux en vol et blocked ne sont pas sélectionnés."""
        if self.pass_lock.locked():
            return
        async with self.pass_lock:
            remaining = 4
            # Si la DB n'a pas pu enregistrer blocked, retenter seulement cette
            # écriture, jamais les fournisseurs en boucle sur la même version.
            for alert_id, (token, code) in list(self.deferred_blocks.items())[:1]:
                remaining -= 1
                try:
                    await self._block(alert_id, token, code)
                    del self.deferred_blocks[alert_id]
                except Exception:
                    # Tourner ces erreurs aussi : une ligne récurrente ne prend
                    # ni toute la passe ni toutes les reprises d'état suivantes.
                    self.deferred_blocks.pop(alert_id, None)
                    self.deferred_blocks[alert_id] = (token, code)
                    logger.error("État de préparation non persisté", exc_info=True)
            async with AsyncSessionLocal() as session:
                ids = await AlertRepository(session).pending_preparation_ids(
                    set(self.tasks) | set(self.deferred_blocks)
                )
            results = await asyncio.gather(
                *(self.prepare(alert_id) for alert_id in ids[:remaining]), return_exceptions=True
            )
            for result in results:
                if isinstance(result, Exception):
                    logger.error("Passe de préparation interrompue", exc_info=result)

    async def _block(self, alert_id: uuid.UUID, token: str, code: PreparationErrorCode) -> Outcome:
        async with AsyncSessionLocal() as session:
            current = await AlertRepository(session).lock_preparation(alert_id, token)
            if current is None:
                return "superseded"
            request = preparation_request(current)
            assert request is not None
            request.state, request.error_code = "blocked", code
            current.preparation = request.stored()
            await session.commit()
        return "blocked"

    async def _run(self, alert_id: uuid.UUID) -> Outcome:
        async with self.slots:
            token: str | None = None
            stage: PreparationErrorCode = "unexpected"
            try:
                async with AsyncSessionLocal() as session:
                    repo = AlertRepository(session)
                    alert = await repo.get_by_id(alert_id, refresh=True)
                    if alert is None or alert.deleted_at is not None:
                        return "stopped"
                    request = preparation_request(alert)
                    if request is None:
                        return "completed" if alert.status == AlertStatus.ACTIVE else "stopped"
                    if request.state != "pending" or not request.activation_requested:
                        return "stopped"
                    token = request.token
                    deferred = self.deferred_blocks.get(alert_id)
                    if deferred is not None:
                        if deferred[0] == token:
                            return "blocked"
                        del self.deferred_blocks[alert_id]
                    alert = await repo.lock_preparation(alert_id, token)
                    if alert is None:
                        return "superseded"
                    request.last_attempt_at = datetime.now(UTC)
                    alert.preparation = request.stored()
                    name, isbn, author = alert.name, alert.isbn, request.author_hint
                    await session.commit()

                stage = "catalog_write"
                if isbn:
                    async with AsyncSessionLocal() as session:
                        repo = AlertRepository(session)
                        current = await repo.lock_preparation(alert_id, token)
                        if current is None:
                            return "superseded"
                        book_repo = BookRepository(session)
                        book = await book_repo.get_by_isbn13(isbn)
                        if book is None:
                            book = await book_repo.create(
                                title=name, normalized_title=normalize_text(name), isbn13=isbn
                            )
                        linked = await book_repo.get_all_books_for_alert(alert_id)
                        if not any(existing.id == book.id for existing in linked):
                            await book_repo.link_alert_to_book(alert_id, book.id)
                        book_id = book.id
                        from app.services.book_market_pricing import recalculate_invalidated_series
                        await recalculate_invalidated_series(session)
                        await session.commit()
                    stage = "pricing_write"
                    if await price_book_now(book_id, reason="alert_created") is None:
                        raise PreparationError("pricing_write")
                else:
                    from app.use_cases.ingest_listing import link_books_from_bnf

                    applied = await link_books_from_bnf(
                        name,
                        alert_id,
                        author=author,
                        series=True,
                        wait_for_prices=True,
                        expected_preparation_token=token,
                    )
                    if not applied:
                        return "superseded"

                stage = "activation_write"
                async with AsyncSessionLocal() as session:
                    repo = AlertRepository(session)
                    current = await repo.lock_preparation(alert_id, token)
                    if current is None:
                        return "superseded"
                    # Relecture des caches dans cette session, pas des objets du collecteur.
                    await BookRepository(session).get_all_books_for_alert(alert_id)
                    current.status = AlertStatus.ACTIVE
                    current.preparation = None
                    await session.commit()
                return "completed"
            except asyncio.CancelledError:
                # L'interruption reste pending, reprenable au prochain passage/démarrage.
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, PreparationError) else stage
                if token is not None:
                    try:
                        outcome = await self._block(alert_id, token, code)
                        if outcome == "superseded":
                            return outcome
                    except Exception:
                        self.deferred_blocks[alert_id] = (token, code)
                        logger.error(
                            "État blocked non persisté, version suspendue localement",
                            extra={"alert_id": str(alert_id), "error_code": code},
                            exc_info=True,
                        )
                logger.warning(
                    "Préparation BD bloquée : %s",
                    code,
                    extra={"alert_id": str(alert_id), "error_code": code},
                    exc_info=True,
                )
                return "blocked"


preparation_runner = PreparationRunner()


async def prepare_bd_alert(alert_id: uuid.UUID) -> bool:
    """Entrée commune à la création, à la relance explicite et au scheduler."""
    return await preparation_runner.prepare(alert_id)


async def recover_bd_preparations() -> None:
    """Reprend au plus quatre demandes persistées, sans nouvelle infrastructure."""
    await preparation_runner.recover()
