"""Script de pré-remplissage (seed) du catalogue BD et du cache de prix de référence.

Permet d'initialiser rapidement des BD iconiques avec des prix de marché réels
pour tester immédiatement la détection des bonnes affaires dans Radar Bot.
"""

import asyncio
import logging

from sqlalchemy import select

from app.core.constants import AlertStatus, ComparisonStatus, MatchConfidence
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.db.session import AsyncSessionLocal
from app.services.book_catalog_service import BookCatalogService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar.seed_bd")

SAMPLE_BDS = [
    {
        "title": "Blacksad - Tome 1 : Quelque part entre les ombres",
        "isbn13": "9782205049657",
        "author": "Juan Díaz Canales & Juanjo Guarnido",
        "publisher": "Dargaud",
        "lowest_delivered_price_cents": 1490,  # 14,90 € livré
        "best_provider_name": "momox",
        "best_source_url": "https://www.momox-shop.fr/juan-diaz-canales-blacksad-tome-1-quelque-part-entre-les-ombres-gebundene-ausgabe-M02205049657.html",
    },
    {
        "title": "Astérix le Gaulois - Tome 1",
        "isbn13": "9782012101333",
        "author": "René Goscinny & Albert Uderzo",
        "publisher": "Hachette",
        "lowest_delivered_price_cents": 1250,  # 12,50 € livré
        "best_provider_name": "gibert",
        "best_source_url": "https://www.gibert.com/asterix-le-gaulois-tome-1-105001.html",
    },
    {
        "title": "Tintin au Tibet",
        "isbn13": "9782203001190",
        "author": "Hergé",
        "publisher": "Casterman",
        "lowest_delivered_price_cents": 1390,  # 13,90 € livré
        "best_provider_name": "recyclivre",
        "best_source_url": "https://www.recyclivre.com/shop/9782203001190",
    },
]


async def seed_reference_bds() -> None:
    """Insère ou met à jour les BDs de référence et leur cache de prix de marché."""
    async with AsyncSessionLocal() as session:
        catalog_service = BookCatalogService(session)
        book_repo = BookRepository(session)
        market_repo = MarketPriceRepository(session)

        # Charger toutes les alertes BD actives
        stmt_alerts = (
            select(Alert)
            .join(Category, Alert.category_id == Category.id)
            .where(Category.slug == "bd_comics", Alert.status == AlertStatus.ACTIVE)
        )
        alerts = list((await session.execute(stmt_alerts)).scalars().all())

        for sample in SAMPLE_BDS:
            isbn13 = sample["isbn13"]
            book = await book_repo.get_by_isbn13(isbn13)
            if not book:
                book = await catalog_service.add_book(
                    title=sample["title"],
                    isbn13=isbn13,
                    author=sample["author"],
                    publisher=sample["publisher"],
                )
                logger.info("Livre ajouté au catalogue : %s (ISBN: %s)", book.title, book.isbn13)
            else:
                logger.info(
                    "Livre existant dans le catalogue : %s (ISBN: %s)", book.title, book.isbn13
                )

            # Mettre à jour ou créer le cache de marché (valide 30 jours)
            await market_repo.update_or_create_cache(
                book_id=book.id,
                lowest_delivered_price_cents=sample["lowest_delivered_price_cents"],
                best_provider_name=sample["best_provider_name"],
                best_source_url=sample["best_source_url"],
                confidence=MatchConfidence.HIGH,
                comparison_status=ComparisonStatus.AVAILABLE,
                cache_duration_hours=720,  # 30 jours
            )

            # Associer les alertes actives correspondantes
            norm_title = book.normalized_title.lower()
            for alert in alerts:
                alert_name_norm = alert.name.lower()
                matches_alert = alert_name_norm in norm_title or any(
                    kw.lower() in norm_title for kw in alert.include_keywords
                )
                if matches_alert:
                    await book_repo.link_alert_to_book(
                        alert_id=alert.id,
                        book_id=book.id,
                        confidence=MatchConfidence.HIGH,
                    )
                    logger.info("Alerte '%s' associée au livre '%s'", alert.name, book.title)

        await session.commit()
        logger.info("✅ Catalogue de référence et cache de marché initialisés avec succès !")


if __name__ == "__main__":
    asyncio.run(seed_reference_bds())
