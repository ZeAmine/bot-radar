"""Cycle de vie du panneau marché : lecture du cache et sélection des tomes."""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.labels import BTN_CLOSE
from app.core.constants import AlertStatus
from app.db.models.book import Book
from app.services.market_price_service import (
    MarketPriceService,
    confirmed_series_isbns,
    series_display_volumes,
)
from app.use_cases.view_market_details import MarketPanelData

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
    cache = selected.__dict__.get("market_cache")
    if cache is None or cache.next_refresh_at is None:
        return None
    next_at = cache.next_refresh_at
    aware = next_at if next_at.tzinfo else next_at.replace(tzinfo=UTC)
    return aware if aware > datetime.now(UTC) else None


def _content(view: PanelView) -> tuple[str, InlineKeyboardMarkup]:
    from app.bot.handlers.callbacks import _tome_selection_rows

    panel = view.panel
    books = panel.books_in_series or ([panel.book] if panel.book else [])
    selected = next((b for b in books if b.id == view.selected_id), None)
    volumes = series_display_volumes(books, panel.alert.name if panel.alert else None)
    missing_first = (
        view.alert_id
        and len(books) > 1
        and not any(volume.number == 1 for volume in volumes)
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
        series_title=panel.alert.name if panel.alert else None,
        preferred_isbns=confirmed_series_isbns(panel.alert),
    )
    source_id = view.listing_id or view.alert_id
    rows = (_tome_selection_rows(
        source_id, books, panel.alert.name if panel.alert else None
    ) if source_id and len(books) > 1 else [])
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
        volumes = series_display_volumes(
            panel.books_in_series, panel.alert.name if panel.alert else None,
            confirmed_series_isbns(panel.alert),
        )
        if volumes:
            panel.book = volumes[0].callback_book
            view.selected_id = panel.book.id
    text, keyboard = _content(view)
    view.message = await callback.message.reply(
        text=text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    _panels[_key(view.message)] = view


async def select_panel_tome(callback: CallbackQuery, book_id: uuid.UUID | None) -> bool:
    """Sélectionne seulement un livre appartenant au panneau encore actif."""
    if not isinstance(callback.message, Message):
        return False
    _expire_panels()
    view = _panels.get(_key(callback.message))
    if view is None:
        return False
    books = view.panel.books_in_series or ([view.panel.book] if view.panel.book else [])
    series_title = view.panel.alert.name if view.panel.alert else None
    if book_id is not None and not any(
        b.id == book_id and series_display_volumes([b], series_title)
        for b in books
    ):
        await callback.answer("Tome introuvable.")
        return True
    view.selected_id = book_id
    await render_panel(view)
    await callback.answer()
    return True
