"""Cycle de vie du panneau marché : cache immédiat et actualisation progressive."""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from time import monotonic

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.labels import BTN_CLOSE
from app.core.tasks import safe_create_task
from app.db.session import AsyncSessionLocal
from app.services.book_market_pricing import price_book_now
from app.services.market_price_service import MarketPriceService, extract_tome_number_from_title
from app.use_cases.view_market_details import MarketPanelData, ViewMarketDetailsUseCase

logger = logging.getLogger(__name__)


@dataclass
class PanelView:
    """Contexte d'un message, indépendant des sessions de requête Telegram."""

    message: Message
    panel: MarketPanelData
    listing_id: uuid.UUID | None = None
    alert_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    selected_id: uuid.UUID | None = None
    closed: bool = False
    refreshing: bool = True
    failed: bool = False
    created_at: float = field(default_factory=monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task[None] | None = None


_panels: dict[tuple[int, int], PanelView] = {}
PANEL_TTL_SECONDS = 15 * 60
MAX_OPEN_PANELS = 64


def _key(message: Message) -> tuple[int, int]:
    return message.chat.id, message.message_id


def close_panel(message: Message) -> None:
    """Invalide le panneau avant sa suppression, même si une collecte est en vol."""
    view = _panels.pop(_key(message), None)
    if view:
        view.closed = True
        if view.task and not view.task.done():
            view.task.cancel()


def _expire_panels() -> None:
    for view in list(_panels.values()):
        if monotonic() - view.created_at > PANEL_TTL_SECONDS:
            close_panel(view.message)
    while len(_panels) >= MAX_OPEN_PANELS:
        close_panel(next(iter(_panels.values())).message)


def _content(view: PanelView) -> tuple[str, InlineKeyboardMarkup]:
    from app.bot.handlers.callbacks import _tome_selection_rows

    panel = view.panel
    books = panel.books_in_series or ([panel.book] if panel.book else [])
    selected = next((b for b in books if b.id == view.selected_id), None)
    text = MarketPriceService.format_market_details_card(
        listing=panel.listing,
        book=selected,
        books_in_series=books,
        tome_result=panel.tome_result,
        latest_snapshot=panel.latest_snapshot if selected is panel.book else None,
        series_total_cents=panel.series_total_cents,
        catalogue_exhausted=panel.catalogue_exhausted,
    )
    if (
        view.alert_id
        and len(books) > 1
        and not any(extract_tome_number_from_title(b.title) == 1 for b in books)
    ):
        text += "\nTome 1 absent du catalogue ; premier tome connu affiché."
    if view.refreshing:
        text += "\n\n<i>Actualisation en cours…</i>"
    elif view.failed:
        text += "\n\nActualisation impossible ; derniers relevés conservés."
    source_id = view.listing_id or view.alert_id
    rows = _tome_selection_rows(source_id, books) if source_id and len(books) > 1 else []
    if source_id and len(books) > 1:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Tous les tomes", callback_data=f"market_details:{source_id}"
                )
            ]
        )
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
    *,
    user_id: uuid.UUID | None = None,
) -> None:
    """Affiche immédiatement le cache puis lance le travail dans une session dédiée."""
    if not isinstance(callback.message, Message):
        return
    _expire_panels()
    view = PanelView(
        callback.message,
        panel,
        listing_id=panel.listing.id if panel.listing else None,
        alert_id=panel.alert.id if panel.listing is None and panel.alert else None,
        user_id=user_id,
        selected_id=panel.book.id if panel.book else None,
    )
    if (
        panel.book is None
        and panel.books_in_series
        and not (panel.tome_result and panel.tome_result.is_lot)
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
    view.task = safe_create_task(
        _refresh_panel(view), name=f"market_panel_{view.message.message_id}"
    )


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


async def _reload(view: PanelView) -> None:
    async with AsyncSessionLocal() as db:
        use_case = ViewMarketDetailsUseCase(db)
        if view.alert_id and view.user_id:
            panel = await use_case.execute_for_alert(view.alert_id, view.user_id)
        elif view.listing_id:
            panel = await use_case.execute(view.listing_id, refresh=False)
        else:
            panel = None
        if panel is not None:
            if view.selected_id:
                panel.book = next(
                    (b for b in panel.books_in_series or [] if b.id == view.selected_id),
                    panel.book,
                )
                if panel.book:
                    panel.latest_snapshot = await use_case.market_repo.get_latest_snapshot(
                        panel.book.id
                    )
            view.panel = panel


async def _refresh_panel(view: PanelView) -> None:
    try:
        books = view.panel.books_in_series or ([view.panel.book] if view.panel.book else [])
        if not books and view.panel.alert:
            async with AsyncSessionLocal() as db:
                await ViewMarketDetailsUseCase(db).resolve_alert_books(
                    view.panel.alert.id, view.user_id,
                )
            await _reload(view)
            books = view.panel.books_in_series or ([view.panel.book] if view.panel.book else [])
            if books:
                view.selected_id = view.panel.book.id if view.panel.book else books[0].id
        remaining = {book.id for book in books}
        while remaining and not view.closed:
            # La sélection courante passe en tête ; deux livres au plus par vague.
            order = sorted(remaining, key=lambda book_id: book_id != view.selected_id)
            wave = order[:1] if view.refreshing and view.selected_id in remaining else order[:2]
            outcomes = await asyncio.gather(
                *(price_book_now(book_id, reason="market_panel") for book_id in wave)
            )
            view.failed = view.failed or any(outcome is None for outcome in outcomes)
            remaining.difference_update(wave)
            await _reload(view)
            view.refreshing = bool(remaining)
            await render_panel(view)
            if remaining:
                await asyncio.sleep(1.2)
        view.refreshing = False
        await render_panel(view)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Actualisation du panneau marché impossible")
        view.failed = True
        view.refreshing = False
        await render_panel(view)
