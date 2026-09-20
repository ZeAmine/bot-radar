import asyncio
import logging
import random
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ComparisonStatus
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.db.session import AsyncSessionLocal
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


async def refresh_book_prices_task(
    registry: ProviderRegistry | None = None,
    session: AsyncSession | None = None,
) -> dict[str, int]:
    """Rafraîchit les prix de marché pour tous les livres associés à des alertes BD actives."""
    if registry is None:
        registry = ProviderRegistry()

    stats = {"processed": 0, "available": 0, "unavailable": 0, "errors": 0}

    # Utiliser la session fournie ou en ouvrir une nouvelle
    owns_session = session is None
    db_session = session if session else AsyncSessionLocal()

    try:
        book_repo = BookRepository(db_session)
        market_repo = MarketPriceRepository(db_session)

        # 1. Charger tous les livres associés à des alertes BD actives
        books = await book_repo.get_books_for_active_bd_alerts()
        logger.info("Début du rafraîchissement des prix BD pour %d livres.", len(books))

        for book in books:
            book_id = str(book.id)
            book_title = book.title
            stats["processed"] += 1
            try:
                # 2. Interroger tous les providers activés en parallèle contrôlé (timeout 20s)
                offers = await asyncio.wait_for(
                    registry.fetch_all_offers(
                        isbn13=book.isbn13,
                        title=book.title,
                        author=book.author,
                        edition=book.edition,
                    ),
                    timeout=20.0,
                )

                # 3. Enregistrer chaque offre comme snapshot
                for offer in offers:
                    await market_repo.save_snapshot(book.id, offer)

                # 4. Sélectionner l'offre la plus basse au total livré
                best_offer = registry.select_best_offer(offers)

                # 5. Mettre à jour le cache consolidé
                if best_offer:
                    await market_repo.update_or_create_cache(
                        book_id=book.id,
                        lowest_delivered_price_cents=best_offer.delivered_price_cents,
                        best_provider_name=(
                            best_offer.raw_payload.get("best_merchant")
                            if best_offer.raw_payload
                            else None
                        )
                        or best_offer.provider_name,
                        best_source_url=best_offer.source_url,
                        confidence=best_offer.confidence,
                        comparison_status=ComparisonStatus.AVAILABLE,
                        cache_duration_hours=24,
                        historical_low_1y_cents=(
                            best_offer.raw_payload.get("historical_low_1y_cents")
                            if best_offer.raw_payload
                            else None
                        ),
                    )
                    stats["available"] += 1
                else:
                    # Ne pas écraser un cache déjà AVAILABLE et non expiré en cas d'échec temporaire
                    existing_cache = book.market_cache
                    now_utc = datetime.now(UTC)
                    if (
                        existing_cache
                        and existing_cache.comparison_status == ComparisonStatus.AVAILABLE
                        and (existing_cache.expires_at is None or existing_cache.expires_at > now_utc)
                    ):
                        logger.warning(
                            "Aucune offre trouvée pour '%s' mais cache valide conservé.", book_title
                        )
                        existing_cache.checked_at = now_utc
                        stats["available"] += 1
                    else:
                        await market_repo.update_or_create_cache(
                            book_id=book.id,
                            lowest_delivered_price_cents=None,
                            best_provider_name=None,
                            best_source_url=None,
                            confidence=None,
                            comparison_status=ComparisonStatus.UNAVAILABLE,
                            cache_duration_hours=6,
                        )
                        stats["unavailable"] += 1

                await db_session.commit()
                await asyncio.sleep(random.uniform(3.0, 4.5))

            except Exception as exc:
                await db_session.rollback()
                logger.error(
                    "Erreur lors du rafraîchissement pour le livre ID %s (%s): %s",
                    book_id,
                    book_title,
                    exc,
                )
                stats["errors"] += 1

        logger.info(
            "Rafraîchissement terminé : %d traités, %d avec prix, %d indisponibles, %d erreurs.",
            stats["processed"],
            stats["available"],
            stats["unavailable"],
            stats["errors"],
        )
        return stats

    finally:
        if owns_session:
            await db_session.close()
