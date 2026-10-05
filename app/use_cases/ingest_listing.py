"""Cas d'usage : Ingestion, matching déterministe et orchestration d'alerte."""

import logging
import sys
import uuid
from typing import Any

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.bot_instance import bot as default_bot
from app.core.constants import CategorySlug
from app.core.tasks import safe_create_task
from app.db.models.book import Book
from app.db.models.bot_event import BotEvent
from app.db.models.listing import Listing
from app.db.repositories.book_repo import BookRepository, resolve_loaded_books
from app.db.repositories.listing_repo import ListingRepository
from app.domain.rules.listing_match import evaluate_listing_against_alert
from app.schemas.listing import (
    ListingIngestPayload,
    ListingIngestResponse,
    ListingMatchItem,
)
from app.services.book_catalog_service import BookCatalogService
from app.services.book_market_pricing import schedule_book_pricing
from app.services.market_health import classify_sent_cote_state, record_sent_cote_state
from app.services.market_price_service import (
    PENDING_COTE_LINE,
    SEARCHING_BOOK_LINE,
    MarketPriceService,
    has_usable_price,
)
from app.services.telegram_service import send_listing_alert

logger = logging.getLogger("radar.use_cases.ingest_listing")


async def link_books_from_bnf(
    title: str,
    alert_id: uuid.UUID,
    author: str | None = None,
    *,
    series: bool = False,
) -> None:
    """Lie les notices BNF d'une œuvre et les cote, sans retarder l'envoi Telegram.

    L'entrée est le **nom de l'alerte** (l'œuvre surveillée), pas le titre brut de
    l'annonce : « The Grocery : l'intégrale » n'est pas une notice, « The Grocery »
    en est une. Le repli BNF échouait précisément là et laissait le livre sans ISBN.
    L'auteur, quand il est connu, accompagne le titre ; `lookup_by_title_and_author`
    attend la forme « Titre par Auteur ».

    `series=True` demande à la BNF la série entière : une alerte suivie par son nom
    n'a aucune fiche, et sa première annonce partait sans cote tant qu'aucun tome
    n'avait été lié. Chaque notice liée est cotée si elle n'a pas déjà une cote
    utilisable, une seule fois par livre (le garde de `schedule_book_pricing`).
    """
    from app.db.session import AsyncSessionLocal
    from app.services.book_metadata_service import BookMetadataService

    query = f"{title} par {author}" if author else title
    try:
        service = BookMetadataService()
        if series:
            found = await service.lookup_series_notices(title, author_hint=author)
        else:
            meta = await service.lookup_by_title_and_author(query)
            found = [meta] if meta else []
        notices = [notice for notice in found if notice and notice.isbn13]
        if not notices:
            return

        to_price: list[uuid.UUID] = []
        async with AsyncSessionLocal() as session:
            book_repo = BookRepository(session)
            catalog = BookCatalogService(session)
            for notice in notices:
                isbn13 = notice.isbn13
                if not isbn13:
                    continue
                book = await book_repo.get_by_isbn13(isbn13)
                if not book:
                    book = await catalog.add_book(
                        title=notice.title,
                        isbn13=isbn13,
                        author=notice.author,
                        publisher=notice.publisher,
                    )
                if not book:
                    continue
                await book_repo.link_alert_to_book(alert_id=alert_id, book_id=book.id)
                # `add_book` rend un livre dont `market_cache` n'est pas chargé : on
                # relit par le dépôt (qui précharge la relation) avant de décider.
                loaded = await book_repo.get_by_id(book.id)
                if loaded is not None and not has_usable_price(loaded.market_cache):
                    to_price.append(book.id)
            await session.commit()
    except Exception as exc:
        logger.warning("Liaison ISBN BNF ignorée pour l'alerte %s : %s", alert_id, exc)
        return

    # La cote part en arrière-plan : c'est elle qui fera apparaître la ligne de prix
    # sur les alertes suivantes de cet ouvrage.
    for book_id in to_price:
        schedule_book_pricing(book_id, reason="bnf_link")


def comparison_line_from_loaded_books(
    listing: Listing,
    alert: Any,
) -> tuple[str | None, bool, Book | None]:
    """Cote synchrone depuis les livres déjà chargés. False si le catalogue est vide.

    Le livre résolu est rendu avec la ligne : c'est lui qui sera coté en arrière-plan
    si sa cote est encore inconnue, une fois l'alerte partie.
    """
    links = getattr(alert, "alert_books", None)
    books = []
    if isinstance(links, list):
        books = [link.book for link in links if getattr(link, "book", None) is not None]
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
    return line or _pending_cote_line(book), True, book


def _pending_cote_line(book: Book | None) -> str | None:
    """Ligne d'attente quand un livre est identifié mais pas encore côté.

    Le silence faisait croire à l'absence de prix. Le livre, lui, n'est cote qu'en
    arrière-plan : l'alerte ne doit pas attendre un marchand.
    """
    return PENDING_COTE_LINE if book is not None else None


async def _link_bd_catalog_background(
    *,
    alert_id: uuid.UUID,
    alert_name: str,
    alert_isbn: str | None,
    listing_title: str,
    listing_description: str | None,
    listing_isbn13: str | None,
) -> None:
    """Lie le catalogue BD après l'alerte, dans une session dédiée."""
    from app.db.session import AsyncSessionLocal

    listing = type(
        "ListingRef",
        (),
        {
            "title": listing_title,
            "description": listing_description,
            "isbn13": listing_isbn13,
            "category_slug": CategorySlug.BD_COMICS.value,
            "item_price_cents": 0,
            "estimated_total_cents": None,
        },
    )()
    alert = type("AlertRef", (), {"id": alert_id, "name": alert_name, "isbn": alert_isbn})()
    book_id: uuid.UUID | None = None
    needs_price = False
    try:
        async with AsyncSessionLocal() as session:
            book = await ProcessListingUseCase(session)._resolve_bd_market_comparison(
                listing, alert
            )
            if book is not None:
                book_id = book.id
                # Le livre lié peut venir d'un `add_book` dont `market_cache` n'est pas
                # chargé : on le relit par le dépôt (qui précharge la relation) avant
                # de décider s'il faut le coter.
                loaded = await BookRepository(session).get_by_id(book.id)
                needs_price = loaded is not None and not has_usable_price(
                    loaded.market_cache
                )
            await session.commit()
    except Exception as exc:
        logger.debug("Liaison catalogue BD différée ignorée pour %s : %s", alert_id, exc)
        return

    # La résolution de fond cote le livre qu'elle vient de lier. La cotation part
    # après le commit : une cote lancée plus tôt ouvrirait une session qui ne verrait
    # pas encore le livre, et le job glissant finirait par être le seul à le coter.
    if book_id is not None and needs_price:
        schedule_book_pricing(book_id, reason="bd_resolved")


def _alert_has_telegram_chat(alert: Any) -> bool:
    """Vrai si l'alerte a un destinataire Telegram à qui envoyer."""
    user = getattr(alert, "user", None)
    if user is None:
        return False
    return bool(getattr(user, "telegram_user_id", None))


def _get_send_listing_alert() -> Any:
    """Résout dynamiquement la fonction d'envoi d'alerte pour préserver les mocks de tests."""
    ingest_module = sys.modules.get("app.api.routes.ingest")
    if ingest_module and hasattr(ingest_module, "send_listing_alert"):
        return ingest_module.send_listing_alert
    return send_listing_alert


# Alerte à notifier, ligne de cote, état du catalogue et livre résolu.
PendingSend = tuple[Any, Any, str | None, bool, Book | None]


class ProcessListingUseCase:
    """Orchestre la déduplication, le matching, la comparaison de prix et les alertes."""

    def __init__(
        self,
        db: AsyncSession,
        bot: Bot | None = None,
    ) -> None:
        self.db = db
        self.bot = bot or default_bot
        self.listing_repo = ListingRepository(db)

    async def execute(
        self,
        payload: ListingIngestPayload,
        raw_payload: dict[str, Any],
    ) -> ListingIngestResponse:
        """Traite l'annonce avec garantie de latence minimale sur l'envoi d'alerte."""
        # 1. Vérification des doublons
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
            listing = await self.listing_repo.create_from_payload(payload, raw_dict=raw_payload)
            is_duplicate = False

        active_alerts = await self.listing_repo.get_active_alerts_by_category(payload.category_slug)
        pending_sends: list[PendingSend] = []
        for alert in active_alerts:
            match_result = evaluate_listing_against_alert(
                alert=alert,
                listing_category_slug=listing.category_slug,
                listing_normalized_title=listing.normalized_title,
                listing_normalized_description=listing.description,
                listing_condition=listing.condition,
                listing_item_price_cents=listing.item_price_cents,
                listing_isbn13=listing.isbn13,
                listing_raw_url=listing.url,
            )
            if not match_result.is_match or alert.id in notified_ids:
                continue
            market_line = None
            catalog_ready = False
            resolved_book: Book | None = None
            if listing.category_slug == CategorySlug.BD_COMICS.value:
                market_line, catalog_ready, resolved_book = comparison_line_from_loaded_books(
                    listing, alert
                )
                if not catalog_ready:
                    market_line, catalog_ready, resolved_book = (
                        await self._comparison_from_catalog_isbn(listing, alert)
                    )
                # Aucune alerte BD ne part sans bloc de cote : livre identifié → cote
                # en cours ; sinon → recherche de l'ouvrage en cours (la résolution
                # de fond va réellement la lancer, cf. `if not catalog_ready`).
                if market_line is None:
                    market_line = (
                        PENDING_COTE_LINE if catalog_ready else SEARCHING_BOOK_LINE
                    )
            pending_sends.append(
                (alert, match_result, market_line, catalog_ready, resolved_book)
            )

        if is_duplicate and not pending_sends and not pending_ids:
            return ListingIngestResponse(
                listing_id=listing.id,
                is_duplicate=True,
                estimated_total_cents=listing.estimated_total_cents,
                matches=[],
            )

        for alert, match_result, _line, _ready, _book in pending_sends:
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

        match_items: list[ListingMatchItem] = []
        needs_retry = False
        books_to_price: set[uuid.UUID] = set()
        send_alert_fn = _get_send_listing_alert()
        for alert, _match_result, market_line, catalog_ready, book in pending_sends:
            notification_sent = False
            if _alert_has_telegram_chat(alert):
                alert_kws = alert.include_keywords or ([alert.name] if alert.name else None)
                notification_sent = await send_alert_fn(
                    bot=self.bot,
                    chat_id=alert.user.telegram_user_id,
                    listing=listing,
                    market_comparison_line=market_line,
                    alert_keywords=alert_kws,
                    alert=alert,
                )
                if notification_sent:
                    await self.listing_repo.mark_notified([(alert.id, listing.id)])
                    await self.db.commit()
                else:
                    needs_retry = True
            if listing.category_slug == CategorySlug.BD_COMICS.value:
                if notification_sent:
                    # Mesure après l'envoi, jamais avant : le diagnostic porte sur la
                    # ligne que l'utilisateur a réellement reçue.
                    safe_create_task(
                        record_sent_cote_state(classify_sent_cote_state(market_line)),
                        name=f"cote_state_{alert.id}",
                    )
                if book is not None and not has_usable_price(book.market_cache):
                    books_to_price.add(book.id)
                if not catalog_ready:
                    safe_create_task(
                        _link_bd_catalog_background(
                            alert_id=alert.id,
                            alert_name=alert.name,
                            alert_isbn=getattr(alert, "isbn", None),
                            listing_title=listing.title,
                            listing_description=listing.description,
                            listing_isbn13=listing.isbn13,
                        ),
                        name=f"bd_catalog_{alert.id}",
                    )
            match_items.append(
                ListingMatchItem(
                    alert_id=alert.id,
                    alert_name=alert.name,
                    notification_sent=notification_sent,
                )
            )

        # Toutes les alertes sont parties : la cote des livres qui en sont dépourvus
        # peut enfin partir chez les marchands, pour que la prochaine l'affiche.
        for book_id in books_to_price:
            schedule_book_pricing(book_id, reason="alert_sent")

        return ListingIngestResponse(
            listing_id=listing.id,
            is_duplicate=is_duplicate,
            estimated_total_cents=listing.estimated_total_cents,
            matches=match_items,
            needs_retry=needs_retry,
        )

    async def _comparison_from_catalog_isbn(
        self,
        listing: Listing,
        alert: Any,
    ) -> tuple[str | None, bool, Book | None]:
        """Une lecture ISBN indexée si le livre est déjà au catalogue. Pas de BNF."""
        isbn = listing.isbn13 or getattr(alert, "isbn", None)
        if not isbn:
            return None, False, None
        book_repo = BookRepository(self.db)
        book = await book_repo.get_by_isbn13(isbn)
        if book is None:
            return None, False, None
        await book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)
        line = MarketPriceService.get_comparison_summary(
            listing=listing,
            book=book,
            books_in_series=[book],
        )
        return line or _pending_cote_line(book), True, book

    async def _resolve_bd_market_comparison(self, listing: Listing, alert: Any) -> Book | None:
        """Résout le livre en local. La recherche BNF part en tâche de fond.

        Rend le livre lié (ou ``None``) et non une ligne : c'est l'appelant qui, une
        fois la transaction commitée, déclenche sa cotation. Le faire ici ouvrirait
        une session avant le commit, qui ne verrait pas encore le livre.
        """
        book_repo = BookRepository(self.db)
        catalog_service = BookCatalogService(self.db)

        book = None
        tome_result = None

        if not alert.isbn:
            book, _books_in_series, tome_result = await book_repo.resolve_book_for_listing(
                alert_id=alert.id,
                alert_name=alert.name,
                listing_title=listing.title,
                listing_description=listing.description,
            )

            if not book:
                discovered_isbn = listing.isbn13
                has_lot = bool(tome_result and tome_result.is_lot and tome_result.lot_tomes)
                if not discovered_isbn and not has_lot:
                    safe_create_task(
                        link_books_from_bnf(
                            alert.name,
                            alert.id,
                            author=getattr(alert, "author", None),
                        ),
                        name=f"bnf_link_{alert.id}",
                    )

                if discovered_isbn:
                    book = await book_repo.get_by_isbn13(discovered_isbn)
                    if not book:
                        book = await catalog_service.add_book(
                            title=listing.title,
                            isbn13=discovered_isbn,
                        )
                    if book:
                        await book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)
        else:
            book = await book_repo.get_book_for_alert(alert.id)
            if not book and alert.isbn:
                book = await book_repo.get_by_isbn13(alert.isbn)
                if not book:
                    book = await catalog_service.add_book(
                        title=alert.name,
                        isbn13=alert.isbn,
                    )
                if book:
                    await book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)

        return book
