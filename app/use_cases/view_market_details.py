"""Cas d'usage : résolution des données affichées par le panneau « Prix du marché ».

Un clic sur le bouton ne doit pas afficher n'importe quoi : le livre d'une
annonce se résout en cascade — l'alerte, son ISBN, la détection de tome, le
catalogue, puis la cotation à la demande si le cache ne suffit pas. Cette
cascade est de la logique métier : elle vit ici, éprouvée sans aiogram, et le
handler ne fait que présenter ce que ce module a résolu.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.models.listing import Listing
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.db.repositories.alert_repo import AlertRepository, BibliographicSnapshot
from app.db.repositories.book_repo import BookRepository, resolve_loaded_books
from app.db.repositories.listing_repo import ListingRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.domain.rules.catalogue_search import is_uncatalogued
from app.services.book_catalog_service import BookCatalogService
from app.services.book_market_pricing import price_book_now
from app.services.book_metadata_service import BookMetadataService
from app.services.market_price_service import extract_tome_number_from_title, has_usable_price
from app.services.tome_extractor import TomeMatchResult

logger = logging.getLogger(__name__)


@dataclass
class MarketPanelData:
    """Ce que le panneau affiche, déjà résolu.

    ``book`` est le livre **affiché** : celui de l'annonce, ou l'unique tome de
    la série quand le texte n'en a pas désigné. ``None`` signale une série à
    choisir tome par tome — c'est ce cas qui fait apparaître le clavier de
    sélection. ``series_total_cents`` est le total de série **lu en base**
    (jamais recomposé ici) : ``None`` tant qu'aucune écriture de cote ne l'a posé.
    """

    listing: Listing | None
    book: Book | None
    books_in_series: list[Book] | None
    tome_result: TomeMatchResult | None
    latest_snapshot: MarketPriceSnapshot | None
    series_total_cents: int | None = None
    catalogue_exhausted: bool = False
    alert: Alert | None = None


class ViewMarketDetailsUseCase:
    """Résout le livre d'une annonce et la cote à lui afficher."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.listing_repo = ListingRepository(db)
        self.book_repo = BookRepository(db)
        self.market_repo = MarketPriceRepository(db)

    async def execute(
        self,
        listing_id: uuid.UUID,
        *,
        refresh: bool = True,
    ) -> MarketPanelData | None:
        """Données du panneau, ou ``None`` si l'annonce n'existe pas.

        Une annonce inconnue n'est pas une erreur : c'est au handler de le dire
        à l'utilisateur, pas à ce module de choisir son message.
        """
        listing = await self.listing_repo.get_by_id(listing_id)
        if listing is None:
            return None

        alert = await self.listing_repo.get_alert_for_listing(listing.id)
        book, books_in_series, tome_result = await self._resolve_book(listing, alert)
        target_isbn = (
            (alert.isbn if alert else None) or listing.isbn13 or (book.isbn13 if book else None)
        )

        if refresh and not book and not books_in_series:
            catalog = BookCatalogService(self.db)
            book, _ = await catalog.identify_book(isbn13=target_isbn, title=listing.title)

        # Le tome précis de l'annonce manque encore aux livres de la série :
        # l'ISBN du tome vient alors de la notice du titre.
        if refresh and not book and tome_result and tome_result.tome_number:
            verdict = await BookMetadataService().lookup_by_title_and_author(listing.title)
            meta = verdict.notice
            if meta and meta.isbn13:
                catalog = BookCatalogService(self.db)
                book = await catalog.add_book(
                    title=listing.title,
                    isbn13=meta.isbn13,
                    author=meta.author,
                    publisher=meta.publisher,
                )
                if alert:
                    await self.book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)
                if books_in_series is not None:
                    books_in_series.append(book)
                target_isbn = meta.isbn13
                from app.services.book_market_pricing import recalculate_invalidated_series
                await recalculate_invalidated_series(self.db)
                await self.db.commit()

        if refresh:
            book = await self._refresh_price_if_needed(
                listing=listing,
                alert=alert,
                book=book,
                books_in_series=books_in_series,
                target_isbn=target_isbn,
            )

        # Le total vient de l'alerte, et la cotation qui vient peut-être d'avoir
        # lieu dans `_refresh_price_if_needed` vient de le réécrire dans sa propre
        # session : on relit plutôt que d'afficher la valeur du début d'entrée.
        series_total: int | None = None
        catalogue_exhausted = False
        if alert is not None:
            await self.db.refresh(alert)
            series_total = alert.series_total_cents
            # Le verdict de recherche est lu en base comme le total : le panneau
            # l'affiche, il ne le recalcule pas.
            catalogue_exhausted = is_uncatalogued(alert)

        # Un livre non résolu dans une série d'un seul tome est ce tome : le
        # panneau n'a pas besoin d'un choix qui n'en est pas un.
        target_book = book
        if (
            not target_book
            and books_in_series
            and len(books_in_series) == 1
            and not (tome_result and tome_result.is_lot)
        ):
            target_book = books_in_series[0]

        latest_snapshot = None
        if target_book is not None:
            latest_snapshot = await self.market_repo.get_latest_snapshot(target_book.id)

        return MarketPanelData(
            listing=listing,
            book=target_book,
            books_in_series=books_in_series,
            tome_result=tome_result,
            latest_snapshot=latest_snapshot,
            series_total_cents=series_total,
            catalogue_exhausted=catalogue_exhausted,
            alert=alert,
        )

    async def execute_for_alert(
        self,
        alert_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> MarketPanelData | None:
        """Lit la cote d'une alerte appartenant à l'utilisateur, sans I/O externe."""
        alert = await AlertRepository(self.db).get_by_id(alert_id, user_id=user_id)
        if alert is None or alert.category.slug != "bd_comics":
            return None
        books = await self.book_repo.get_all_books_for_alert(alert.id)
        books.sort(key=lambda b: (extract_tome_number_from_title(b.title) or 999, b.title))
        book = books[0] if books else None
        if alert.isbn:
            book = await self.book_repo.get_book_for_alert(alert.id)
            books = [book] if book else []
        snapshot = await self.market_repo.get_latest_snapshot(book.id) if book else None
        return MarketPanelData(
            listing=None,
            book=book,
            books_in_series=books,
            tome_result=None,
            latest_snapshot=snapshot,
            series_total_cents=alert.series_total_cents,
            catalogue_exhausted=is_uncatalogued(alert),
            alert=alert,
        )

    async def resolve_alert_books(
        self,
        alert_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> None:
        """Identifie et lie les ouvrages d'une alerte pour une consultation explicite."""
        alert = await AlertRepository(self.db).get_by_id(alert_id, user_id=user_id)
        if alert is None:
            return
        snapshot = BibliographicSnapshot.capture(alert)
        books = await self.book_repo.get_all_books_for_alert(alert.id)
        if books and (alert.isbn or all(extract_tome_number_from_title(b.title) for b in books)):
            return
        if alert.isbn:
            book, _ = await BookCatalogService(self.db).identify_book(
                isbn13=alert.isbn,
                title=alert.name,
            )
            if book and book.isbn13 == alert.isbn:
                if await AlertRepository(self.db).lock_bibliographic_result(
                    alert.id, snapshot
                ) is None:
                    return
                await self.book_repo.link_alert_to_book(alert.id, book.id)
                from app.services.book_market_pricing import recalculate_invalidated_series
                await recalculate_invalidated_series(self.db)
                await self.db.commit()
        else:
            from app.use_cases.ingest_listing import link_books_from_bnf
            await link_books_from_bnf(alert.name, alert.id, series=True,
                                      expected_snapshot=snapshot)

    async def _resolve_book(
        self,
        listing: Listing,
        alert: Alert | None,
    ) -> tuple[Book | None, list[Book] | None, TomeMatchResult | None]:
        """Livre rattaché à l'alerte, série complète et lecture du texte de l'annonce.

        Sans alerte, rien n'est rattaché : c'est le catalogue qui tranchera plus
        loin. Une alerte ISBN ne connaît qu'un ouvrage — sa série n'est pas
        chargée, il n'y a rien à faire choisir.
        """
        if alert is None:
            return None, None, None
        if alert.isbn:
            book = await self.book_repo.get_book_for_alert(alert.id)
            selected, books, tome_result = resolve_loaded_books(
                alert_name=alert.name,
                listing_title=listing.title,
                listing_description=listing.description,
                books=[book] if book else [],
            )
            return selected or (book if not tome_result.is_lot else None), books, tome_result

        book, series, tome_result = await self.book_repo.resolve_book_for_listing(
            alert_id=alert.id,
            alert_name=alert.name,
            listing_title=listing.title,
            listing_description=listing.description,
        )
        if not book and series:
            # Le texte n'a pas désigné de tome : l'annonce peut être un lot
            # reconnu sur la photo, seule source fiable d'un tel lot.
            payload = listing.raw_payload or {}
            raw_lot = payload.get("lot_tomes")
            lot_tomes = (
                [int(t) for t in raw_lot if str(t).lstrip("-").isdigit()]
                if isinstance(raw_lot, list)
                else []
            )
            if payload.get("is_lot") and lot_tomes:
                tome_result = TomeMatchResult(
                    is_lot=True,
                    lot_tomes=lot_tomes,
                    confidence="high",
                )
        return book, series, tome_result

    async def _refresh_price_if_needed(
        self,
        *,
        listing: Listing,
        alert: Alert | None,
        book: Book | None,
        books_in_series: list[Book] | None,
        target_isbn: str | None,
    ) -> Book | None:
        """Cote le livre quand le cache ne suffit pas ; n'élève jamais.

        Renvoie le livre **à afficher**, qui n'est pas toujours celui reçu : sans
        livre résolu, le rafraîchissement en crée un — c'est lui que le panneau
        doit montrer, pas le ``None`` de départ.

        Un marchand muet ne fait pas échouer le panneau : il le laisse sans
        prix neuf, ce qui est déjà arrivé et se rattrape à la passe suivante.
        """
        if book is not None:
            if has_usable_price(book.market_cache):
                return book
        elif books_in_series:
            # Une série sans livre résolu a ses propres cotations, tome par tome.
            return book

        try:
            # La recherche se fait sur le nom de l'alerte, qui est aussi le titre
            # retenu pour la fiche : sans ISBN, c'est ce qui fait converger deux
            # ouvertures du panneau vers le même livre au lieu d'en créer un par
            # clic.
            fallback_title = alert.name if alert else listing.title
            catalog = BookCatalogService(self.db)
            if not book:
                identified, _ = await catalog.identify_book(
                    isbn13=target_isbn,
                    title=fallback_title,
                )
                book = identified or await catalog.add_book(
                    title=fallback_title,
                    isbn13=target_isbn,
                )
                if alert:
                    await self.book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)
            # Le livre doit être écrit avant que la cotation ne lise sa session.
            from app.services.book_market_pricing import recalculate_invalidated_series
            await recalculate_invalidated_series(self.db)
            await self.db.commit()
            cache = await price_book_now(book.id, reason="market_button")
            if cache is not None:
                book.market_cache = cache
        except Exception as exc:
            logger.debug(
                "Rafraîchissement direct du marché ignoré pour l'annonce %s : %s",
                listing.id,
                exc,
            )
        return book
