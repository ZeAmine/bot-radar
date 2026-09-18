"""Script de réinitialisation et recalcul complet du cache de prix de marché pour les 102 alertes.

Actions :
1. Assure que chaque alerte active est associée à une fiche Book propre dans le catalogue avec son ISBN certifié (ou None pour les séries).
2. Vide les tables book_market_cache, market_price_snapshots, alert_books, books.
3. Exécute le rafraîchissement des prix du marché pour tous les livres associés aux 102 alertes BD.
"""

import asyncio
import logging
import uuid
from sqlalchemy import text, select
from app.db.session import AsyncSessionLocal
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.category import Category
from app.core.constants import AlertStatus, MatchConfidence
from app.services.matcher_service import normalize_text as normalize_title
from app.tasks.refresh_book_prices import refresh_book_prices_task

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('radar.reset_and_refresh')


async def main() -> None:
    logger.info("Démarrage de la réinitialisation et du recalcul du cache marché...")

    async with AsyncSessionLocal() as session:
        # 1. Récupérer toutes les alertes actives
        stmt = (
            select(Alert)
            .join(Category, Alert.category_id == Category.id)
            .where(Alert.status == AlertStatus.ACTIVE)
            .order_by(Alert.name)
        )
        res = await session.execute(stmt)
        alerts = list(res.scalars().all())
        logger.info("Nombre d'alertes actives trouvées : %d", len(alerts))

        # 2. Vider complètement les anciennes tables de catalogue / cache polluées
        logger.info("Purge de alert_books, books, book_market_cache, market_price_snapshots...")
        await session.execute(text("TRUNCATE TABLE market_price_snapshots, book_market_cache, alert_books, books CASCADE;"))
        await session.commit()

        # 3. Créer un Book propre pour chaque alerte (en évitant la collision d'unicité ISBN pour les doublons d'alertes)
        created_books = 0
        book_by_isbn: dict[str, Book] = {}

        for alert in alerts:
            clean_isbn = alert.isbn.strip() if alert.isbn else None
            norm_title = normalize_title(alert.name)

            book = None
            if clean_isbn and clean_isbn in book_by_isbn:
                book = book_by_isbn[clean_isbn]
            else:
                book = Book(
                    id=uuid.uuid4(),
                    isbn13=clean_isbn,
                    title=alert.name,
                    normalized_title=norm_title,
                    language="fr",
                )
                session.add(book)
                await session.flush()
                created_books += 1
                if clean_isbn:
                    book_by_isbn[clean_isbn] = book

            ab = AlertBook(
                alert_id=alert.id,
                book_id=book.id,
                match_confidence=MatchConfidence.HIGH if clean_isbn else MatchConfidence.MEDIUM,
            )
            session.add(ab)

        await session.commit()
        logger.info("Fiches Book créées : %d, AlertBook créés : %d", created_books, len(alerts))

    # 4. Lancer le rafraîchissement complet
    logger.info("Lancement de refresh_book_prices_task pour toutes les alertes...")
    stats = await refresh_book_prices_task()
    logger.info("Recalcul terminé avec succès : %s", stats)


if __name__ == '__main__':
    asyncio.run(main())
