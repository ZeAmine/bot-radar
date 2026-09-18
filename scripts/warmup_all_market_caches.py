"""Script de pré-remplissage et warm-up de tous les caches de marché pour les livres."""

import asyncio
import logging
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.constants import ComparisonStatus
from app.db.models.book import Book
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.db.session import AsyncSessionLocal
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar.warmup")


async def warmup_caches(force_refresh: bool = False, batch_limit: int | None = None):
    provider = ChasseAuxLivresProvider(timeout_seconds=12.0)
    
    async with AsyncSessionLocal() as session:
        market_repo = MarketPriceRepository(session)
        
        # Récupérer tous les livres avec leur cache
        stmt = select(Book).options(selectinload(Book.market_cache)).order_by(Book.title.asc())
        res = await session.execute(stmt)
        books = res.scalars().all()
        
        target_books = []
        for b in books:
            cache = b.market_cache
            needs_fill = (
                force_refresh
                or not cache
                or cache.lowest_delivered_price_cents is None
                or cache.comparison_status != ComparisonStatus.AVAILABLE
            )
            if needs_fill and b.isbn13:
                target_books.append(b)
                
        logger.info("Total livres en base : %d | Livres à remplir : %d", len(books), len(target_books))
        
        if batch_limit:
            target_books = target_books[:batch_limit]
            logger.info("Traitement limité à %d livres pour ce lot.", batch_limit)

        success_count = 0
        fail_count = 0

        for idx, book in enumerate(target_books, start=1):
            title = book.title
            isbn = book.isbn13
            logger.info("[%d/%d] Recherche cote pour '%s' (ISBN: %s)...", idx, len(target_books), title, isbn)
            
            try:
                offers = await provider.get_offers(isbn13=isbn, title=title)
                
                # Retry une fois si aucune offre n'a répondu au premier appel
                if not offers:
                    await asyncio.sleep(2.5)
                    offers = await provider.get_offers(isbn13=isbn, title=title)

                if offers:
                    best = offers[0]
                    await market_repo.save_snapshot(book.id, best)
                    h1y = best.raw_payload.get("historical_low_1y_cents") if best.raw_payload else None
                    
                    await market_repo.update_or_create_cache(
                        book_id=book.id,
                        lowest_delivered_price_cents=best.delivered_price_cents,
                        best_provider_name=best.provider_name,
                        best_source_url=best.source_url,
                        confidence=best.confidence,
                        comparison_status=ComparisonStatus.AVAILABLE,
                        cache_duration_hours=72,
                        historical_low_1y_cents=h1y,
                    )
                    await session.commit()
                    success_count += 1
                    h1y_str = f"{h1y/100:.2f} €" if h1y else "None"
                    logger.info(
                        "  ✓ Succès : %s | Livré: %.2f € (1y: %s) | %s",
                        title,
                        best.delivered_price_cents / 100,
                        h1y_str,
                        best.raw_payload.get("best_merchant") if best.raw_payload else "",
                    )
                else:
                    fail_count += 1
                    logger.warning("  ✗ Aucune offre d'occasion trouvée pour '%s' (ISBN: %s)", title, isbn)
            except Exception as exc:
                fail_count += 1
                logger.error("  ! Erreur pour '%s': %s", title, exc)
                await session.rollback()

            # Pause respectueuse pour éviter les rate limits de Chasse-aux-livres
            await asyncio.sleep(2.5)

        logger.info("Fin du warm-up ! Succès : %d | Échecs : %d", success_count, fail_count)


if __name__ == "__main__":
    import sys
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(warmup_caches(batch_limit=limit))
