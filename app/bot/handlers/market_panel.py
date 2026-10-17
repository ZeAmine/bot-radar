"""Cycle de vie du panneau marché : lecture du cache et sélection des tomes."""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.labels import BTN_CLOSE
from app.core.constants import AlertStatus
from app.core.tasks import safe_create_task
from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.book_repo import BookRepository
from app.db.session import AsyncSessionLocal
from app.services.book_market_pricing import first_collection_is_due, schedule_book_pricing
from app.services.market_health import next_full_pass_at
from app.services.market_price_service import MarketPriceService, extract_tome_number_from_title
from app.services.tome_extractor import TomeExtractor
from app.use_cases.view_market_details import MarketPanelData, ViewMarketDetailsUseCase

logger = logging.getLogger(__name__)


@dataclass
class PanelView:
    """Contexte d'un message, indépendant des sessions de requête Telegram."""

    message: Message
    panel: MarketPanelData
    listing_id: uuid.UUID | None = None
    alert_id: uuid.UUID | None = None
    selected_id: uuid.UUID | None = None
    closed: bool = False
    created_at: float = field(default_factory=monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_panels: dict[tuple[int, int], PanelView] = {}
PANEL_TTL_SECONDS = 15 * 60
MAX_OPEN_PANELS = 64


def _key(message: Message) -> tuple[int, int]:
    return message.chat.id, message.message_id


def close_panel(message: Message) -> None:
    """Invalide le panneau avant sa suppression."""
    view = _panels.pop(_key(message), None)
    if view:
        view.closed = True


def _expire_panels() -> None:
    for view in list(_panels.values()):
        if monotonic() - view.created_at > PANEL_TTL_SECONDS:
            close_panel(view.message)
    while len(_panels) >= MAX_OPEN_PANELS:
        close_panel(next(iter(_panels.values())).message)


def _next_check_for(view: PanelView, selected: Book | None) -> datetime | None:
    """Prochaine passe qui prend réellement ce livre en charge, si elle est connue.

    Une passe complète traite tous les livres des alertes actives ou en pause. Le
    panneau ne l'annonce donc que pour un livre rattaché à une telle alerte : la
    seule existence d'une tâche globale ne prouve pas que ce livre en fasse partie.
    """
    alert = view.panel.alert
    if alert is None or selected is None:
        return None
    if alert.status not in {AlertStatus.ACTIVE, AlertStatus.PAUSED}:
        return None
    if getattr(alert, "deleted_at", None) is not None:
        return None
    return next_full_pass_at()


def _request_first_collection(view: PanelView, book: Book | None) -> None:
    """Demande la première cotation d'un livre qui n'en a jamais reçu.

    Le prédicat écarte une cote exploitable, une collecte réussie sans offre et une
    tentative récente. `schedule_book_pricing` conserve ses propres gardes : une
    collecte déjà en vol pour ce livre n'est pas doublée.
    """
    if book is None:
        return
    alert = view.panel.alert
    if alert is not None and (
        alert.status not in {AlertStatus.ACTIVE, AlertStatus.PAUSED}
        or getattr(alert, "deleted_at", None) is not None
    ):
        return
    if not first_collection_is_due(book):
        return
    schedule_book_pricing(book.id, reason="market_panel")


def _catalogue_attempt_is_recent(alert: Alert | None) -> bool:
    if alert is None:
        return False
    attempted = alert.catalogue_refresh_attempted_at
    if attempted is None:
        return False
    aware = attempted if attempted.tzinfo else attempted.replace(tzinfo=UTC)
    return datetime.now(UTC) - aware < timedelta(hours=24)


def _catalogue_resolution_is_needed(panel: MarketPanelData) -> bool:
    """Le panneau manque-t-il d'un livre ou d'un tome que le catalogue peut fournir ?

    La résolution est la même que celle du planificateur, mais déclenchée depuis le
    panneau lorsqu'aucune première cotation n'est possible sans elle. Elle reste
    limitée aux alertes actives ou en pause, et à une tentative par période de 24 h.
    """
    alert = panel.alert
    if alert is None:
        return False
    if alert.status not in {AlertStatus.ACTIVE, AlertStatus.PAUSED}:
        return False
    if getattr(alert, "deleted_at", None) is not None:
        return False
    books = panel.books_in_series or ([panel.book] if panel.book else [])
    if not books:
        return True
    if alert.isbn:
        return False
    if panel.book is None:
        return True
    if panel.tome_result and panel.tome_result.tome_number is not None:
        wanted = panel.tome_result.tome_number
        return not any(
            extract_tome_number_from_title(book.title) == wanted for book in books
        )
    return (
        extract_tome_number_from_title(panel.book.title) is None
        and first_collection_is_due(panel.book)
    )


_catalogue_in_flight: set[uuid.UUID] = set()


def _request_catalogue_resolution(view: PanelView) -> None:
    """Résout le catalogue manquant sans relancer une requête à chaque ouverture."""
    panel = view.panel
    alert = panel.alert
    if alert is None or not _catalogue_resolution_is_needed(panel):
        return
    if _catalogue_attempt_is_recent(alert) or alert.id in _catalogue_in_flight:
        return
    _catalogue_in_flight.add(alert.id)
    tome_number = (
        panel.tome_result.tome_number
        if panel.tome_result and panel.tome_result.tome_number is not None
        else None
    )
    safe_create_task(
        _resolve_catalogue_once(alert.id, tome_number),
        name=f"market_catalogue_{alert.id}",
    )


async def _resolve_catalogue_once(
    alert_id: uuid.UUID, tome_number: int | None = None
) -> None:
    try:
        async with AsyncSessionLocal() as db:
            alert = await AlertRepository(db).get_by_id(alert_id)
            if (
                alert is None
                or alert.status not in {AlertStatus.ACTIVE, AlertStatus.PAUSED}
                or alert.deleted_at is not None
                or _catalogue_attempt_is_recent(alert)
            ):
                return
            alert_name = alert.name
            alert.catalogue_refresh_attempted_at = datetime.now(UTC)
            await db.commit()
            await ViewMarketDetailsUseCase(db).resolve_alert_books(
                alert_id, force_catalogue=True
            )
            books = await BookRepository(db).get_all_books_for_alert(alert_id)
            if tome_number is not None:
                target = next(
                    (
                        book
                        for book in books
                        if TomeExtractor.extract(
                            book.title, series_key=alert_name
                        ).tome_number
                        == tome_number
                    ),
                    None,
                )
            elif len(books) == 1:
                target = books[0]
            else:
                target = None
            if target is not None and first_collection_is_due(target):
                schedule_book_pricing(target.id, reason="market_panel")
    except Exception:
        logger.warning(
            "Résolution catalogue depuis le panneau impossible pour %s",
            alert_id,
            exc_info=True,
        )
    finally:
        _catalogue_in_flight.discard(alert_id)


def _content(view: PanelView) -> tuple[str, InlineKeyboardMarkup]:
    from app.bot.handlers.callbacks import _tome_selection_rows

    panel = view.panel
    books = panel.books_in_series or ([panel.book] if panel.book else [])
    selected = next((b for b in books if b.id == view.selected_id), None)
    missing_first = (
        view.alert_id
        and len(books) > 1
        and not any(extract_tome_number_from_title(b.title) == 1 for b in books)
    )
    text = MarketPriceService.format_market_details_card(
        listing=panel.listing,
        book=selected,
        books_in_series=books,
        tome_result=panel.tome_result,
        latest_snapshot=panel.latest_snapshot if selected is panel.book else None,
        series_total_cents=panel.series_total_cents,
        catalogue_exhausted=panel.catalogue_exhausted,
        additional_note=(
            "<i>Tome 1 absent du catalogue ; premier tome connu affiché.</i>"
            if missing_first
            else None
        ),
        next_check_at=_next_check_for(view, selected),
    )
    source_id = view.listing_id or view.alert_id
    rows = _tome_selection_rows(source_id, books) if source_id and len(books) > 1 else []
    rows.append([InlineKeyboardButton(text=BTN_CLOSE, callback_data="close_market_panel")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def render_panel(view: PanelView) -> None:
    """Lit la sélection sous verrou : une réponse tardive ne change jamais le tome."""
    async with view.lock:
        if view.closed:
            return
        text, keyboard = _content(view)
        try:
            await view.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                view.closed = True
                logger.warning("Panneau marché non modifiable : %s", type(exc).__name__)


async def open_panel(
    callback: CallbackQuery,
    panel: MarketPanelData,
) -> None:
    """Affiche le panneau depuis le cache puis initie les premiers travaux manquants."""
    if not isinstance(callback.message, Message):
        return
    _expire_panels()
    view = PanelView(
        callback.message,
        panel,
        listing_id=panel.listing.id if panel.listing else None,
        alert_id=panel.alert.id if panel.listing is None and panel.alert else None,
        selected_id=panel.book.id if panel.book else None,
    )
    if (
        panel.book is None
        and panel.books_in_series
        and not (panel.tome_result and panel.tome_result.is_lot)
        and not (panel.tome_result and panel.tome_result.tome_number is not None)
    ):
        books = sorted(
            panel.books_in_series, key=lambda b: extract_tome_number_from_title(b.title) or 999
        )
        panel.book = books[0]
        view.selected_id = books[0].id
    text, keyboard = _content(view)
    view.message = await callback.message.reply(
        text=text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    _panels[_key(view.message)] = view
    _request_first_collection(view, panel.book)
    _request_catalogue_resolution(view)


async def select_panel_tome(callback: CallbackQuery, book_id: uuid.UUID | None) -> bool:
    """Sélectionne seulement un livre appartenant au panneau encore actif."""
    if not isinstance(callback.message, Message):
        return False
    _expire_panels()
    view = _panels.get(_key(callback.message))
    if view is None:
        return False
    books = view.panel.books_in_series or ([view.panel.book] if view.panel.book else [])
    if book_id is not None and not any(b.id == book_id for b in books):
        await callback.answer("Tome introuvable.")
        return True
    view.selected_id = book_id
    await render_panel(view)
    await callback.answer()
    return True
