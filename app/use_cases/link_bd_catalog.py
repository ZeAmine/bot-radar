"""Liaison bibliographique partagée, avec sessions et gardes d'identité explicites."""

import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import alert_latency as latency
from app.core.constants import AlertStatus
from app.core.tasks import safe_create_task
from app.db.models.book import Book
from app.db.repositories.alert_repo import AlertRepository, BibliographicSnapshot
from app.db.repositories.book_repo import BookRepository
from app.domain.entities.alert_preparation import PreparationError, preparation_request
from app.domain.rules.catalogue_search import is_catalogue_exhausted
from app.services.book_catalog_service import BookCatalogService
from app.services.book_market_pricing import (
    price_book_now,
    recalculate_invalidated_series,
    schedule_book_pricing,
)
from app.services.market_price_service import extract_tome_number_from_title, has_usable_price
from app.utils.text import normalize_text

if TYPE_CHECKING:
    from app.services.book_metadata_service import CatalogueVerdict

logger = logging.getLogger("radar.use_cases.ingest_listing")


@latency.observe_background("bibliography_series")
async def link_books_from_bnf(
    title: str,
    alert_id: uuid.UUID,
    author: str | None = None,
    *,
    series: bool = False,
    wait_for_prices: bool = False,
    schedule_prices: bool = True,
    expected_preparation_token: str | None = None,
    expected_snapshot: BibliographicSnapshot | None = None,
    catalogue_sink: "list[CatalogueVerdict] | None" = None,
    initial_volumes: list[dict[str, Any]] | None = None,
    reference_publisher: str | None = None,
) -> bool:
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
    `wait_for_prices=True` réserve l'attente à la création d'une alerte : elle
    prépare ses propres cotes avant d'activer la surveillance, sans attendre
    les tâches d'autres alertes. L'ingestion conserve le mode arrière-plan.

    La recherche rend un **verdict** (`CatalogueVerdict`), et c'est ici qu'il devient
    une donnée de l'alerte : quand toutes les autorités interrogées ont répondu sans
    rien rendre, l'alerte le retient et ses prochaines annonces le disent, au lieu
    d'annoncer une recherche en cours indéfiniment (ADR-041).
    """
    from app.db.session import AsyncSessionLocal
    from app.services.book_metadata_service import (
        BookMetadataService,
        SeriesVolume,
        select_series_volumes,
    )

    query = f"{title} par {author}" if author else title
    try:
        async with AsyncSessionLocal() as session:
            current = await AlertRepository(session).get_by_id(alert_id)
            if current is None or current.name != title or current.status == AlertStatus.DELETED:
                return False
            snapshot = expected_snapshot or BibliographicSnapshot.capture(current)
            if not snapshot.matches(current) or (
                expected_preparation_token is not None
                and snapshot.preparation_token != expected_preparation_token
            ):
                return False
        service = BookMetadataService()
        if series:
            options: dict[str, Any] = {"for_creation": True} if wait_for_prices else {}
            if wait_for_prices and reference_publisher:
                options["reference_publisher"] = reference_publisher
            verdict = await service.lookup_series_notices(
                title, author_hint=author,
                **options,
            )
        else:
            verdict = await service.lookup_by_title_and_author(query)
        if initial_volumes:
            selected = select_series_volumes([
                *(SeriesVolume.from_stored(row) for row in initial_volumes
                  if row.get("isbn13") not in verdict.rejected_isbns), *verdict.volumes,
            ], publisher=reference_publisher)
            verdict = replace(
                verdict, volumes=selected,
                notices=tuple(replace(v.notice, isbn13=v.isbn13,
                                      title=v.notice.volume_title or v.notice.title)
                              for v in selected if v.isbn13),
            )
        if catalogue_sink is not None:
            catalogue_sink.append(verdict)
        notices = [notice for notice in verdict.notices if notice and notice.isbn13]

        to_price: list[uuid.UUID] = []
        async with AsyncSessionLocal() as session:
            # Le verdict s'écrit là où il est connu, et seulement quand il est
            # concluant : une notice arrivée efface l'exhaustion, une autorité muette
            # ne la réécrit pas (elle ne prouve rien, ni dans un sens ni dans l'autre).
            alert_repo = AlertRepository(session)
            current = await alert_repo.lock_bibliographic_result(
                alert_id, snapshot, require_preparation=expected_preparation_token is not None,
            )
            if current is None:
                return False
            if notices:
                await alert_repo.set_book_search_exhausted(alert_id, None)
            elif is_catalogue_exhausted(verdict) and not verdict.volumes:
                await alert_repo.set_book_search_exhausted(alert_id, datetime.now(UTC))

            if wait_for_prices:
                request = preparation_request(current)
                if request is not None:
                    request.series_volumes = [volume.stored() for volume in verdict.volumes]
                    current.preparation = request.stored()
            book_repo = BookRepository(session)
            catalog = BookCatalogService(session)
            if initial_volumes:
                # Remplace les seuls liens de cette préparation par la sélection
                # confirmée, dans la même transaction et sous sa garde de version.
                await book_repo.clear_alert_books(alert_id)
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
                        language=notice.language or "und",
                        publication_year=notice.publication_year,
                        format_book=notice.format_detail,
                    )
                if not book:
                    continue
                if (
                    series
                    and extract_tome_number_from_title(notice.title) is not None
                    and extract_tome_number_from_title(book.title) is None
                ):
                    # Même ISBN certifié, titre de tome enfin explicite : les anciennes
                    # fiches issues d'un titre d'œuvre sont réalignées sans changer d'ouvrage.
                    book.title = notice.title
                    book.normalized_title = normalize_text(notice.title)
                await book_repo.link_alert_to_book(alert_id=alert_id, book_id=book.id)
                # `add_book` rend un livre dont `market_cache` n'est pas chargé : on
                # relit par le dépôt (qui précharge la relation) avant de décider.
                loaded = await book_repo.get_by_id(book.id)
                if loaded is not None and not has_usable_price(loaded.market_cache):
                    to_price.append(book.id)
            if wait_for_prices:
                # Les ISBN déjà confirmés à la création restent liés même si la
                # seconde découverte est muette. Leurs cotes sont préparées aussi.
                for linked in await book_repo.get_all_books_for_alert(alert_id):
                    if linked.id not in to_price and not has_usable_price(linked.market_cache):
                        to_price.append(linked.id)
            await recalculate_invalidated_series(session)
            await session.commit()
    except Exception as exc:
        if expected_preparation_token is not None:
            raise PreparationError("catalog_write") from exc
        logger.warning("Liaison ISBN BNF ignorée pour l'alerte %s : %s", alert_id, exc)
        return False

    # La cote part en arrière-plan : c'est elle qui fera apparaître la ligne de prix
    # sur les alertes suivantes de cet ouvrage.
    if not schedule_prices:
        return True
    if wait_for_prices:
        import asyncio

        # Deux livres par vague : la création attend ses propres cotes seulement.
        for start in range(0, len(to_price), 2):
            results = await asyncio.gather(
                *(
                    price_book_now(book_id, reason="bnf_link")
                    for book_id in to_price[start : start + 2]
                )
            )
            if expected_preparation_token is not None and any(result is None for result in results):
                raise PreparationError("pricing_write")
    else:
        for book_id in to_price:
            schedule_book_pricing(book_id, reason="bnf_link")
    return True


async def resolve_bd_catalog_book(
    session: AsyncSession,
    *,
    alert_id: uuid.UUID,
    alert_name: str,
    alert_isbn: str | None,
    listing_title: str,
    listing_description: str | None,
    author: str | None = None,
    alert_snapshot: BibliographicSnapshot | None = None,
) -> Book | None:
    """Résout le livre en local. La recherche BNF part en tâche de fond.

    Rend le livre lié (ou ``None``) et non une ligne : c'est l'appelant qui, une
    fois la transaction commitée, déclenche sa cotation. Le faire ici ouvrirait
    une session avant le commit, qui ne verrait pas encore le livre.
    """
    book_repo = BookRepository(session)
    catalog_service = BookCatalogService(session)

    book = None
    tome_result = None

    if not alert_isbn:
        book, _books_in_series, tome_result = await book_repo.resolve_book_for_listing(
            alert_id=alert_id,
            alert_name=alert_name,
            listing_title=listing_title,
            listing_description=listing_description,
        )

        if not book:
            has_lot = bool(tome_result and tome_result.is_lot and tome_result.lot_tomes)
            if not has_lot:
                # Une alerte sans ISBN est une série : l'annonce non résolue
                # relance la découverte de **tous** ses tomes, pas seulement
                # d'un titre. Une alerte à ISBN garde sa recherche unitaire.
                # L'appel reste après l'envoi (`safe_create_task`), invariant n°1.
                safe_create_task(
                    link_books_from_bnf(
                        alert_name,
                        alert_id,
                        author=author,
                        series=not alert_isbn,
                        expected_snapshot=alert_snapshot,
                    ),
                    name=f"bnf_link_{alert_id}",
                )

    else:
        book = await book_repo.get_book_for_alert(alert_id)
        if not book and alert_isbn:
            book = await book_repo.get_by_isbn13(alert_isbn)
            if not book:
                book = await catalog_service.add_book(
                    title=alert_name,
                    isbn13=alert_isbn,
                )
            if book:
                await book_repo.link_alert_to_book(alert_id=alert_id, book_id=book.id)

    return book
