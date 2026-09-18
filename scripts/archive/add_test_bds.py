"""Script de test et d'ajout semi-automatique pour Deathbringer et La saga de Grimr."""

import asyncio
import logging
import urllib.parse
from sqlalchemy import select

from app.core.config import settings
from app.core.constants import AlertStatus, CategorySlug, ComparisonStatus, DEFAULT_BD_CONDITIONS, MatchConfidence
from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.db.session import AsyncSessionLocal
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
from app.providers.registry import ProviderRegistry
from app.services.book_metadata_service import BookMetadataService
from app.services.matcher_service import normalize_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar.add_test_bds")

TEST_QUERIES = [
    {"query": "deathbringer de ismael le Grand", "max_price_cents": 5000},
    {"query": "la saga de grimr de Jeremy Morrow", "max_price_cents": 5000},
]


async def run():
    service = BookMetadataService()
    provider = ChasseAuxLivresProvider(timeout_seconds=12.0)

    async with AsyncSessionLocal() as session:
        book_repo = BookRepository(session)
        market_repo = MarketPriceRepository(session)

        # 1. Utilisateur
        allowed_ids = settings.TELEGRAM_ALLOWED_USER_IDS
        user_tg_id = allowed_ids[0] if allowed_ids else 6429948805
        stmt_user = select(User).where(User.telegram_user_id == user_tg_id)
        user = (await session.execute(stmt_user)).scalar_one_or_none()
        if not user:
            logger.error("Utilisateur Telegram %s introuvable !", user_tg_id)
            return

        # 2. Catégorie BD
        stmt_cat = select(Category).where(Category.slug == CategorySlug.BD_COMICS.value)
        cat = (await session.execute(stmt_cat)).scalar_one_or_none()
        if not cat:
            logger.error("Catégorie BD introuvable !")
            return

        for item in TEST_QUERIES:
            raw_q = item["query"]
            max_cents = item["max_price_cents"]
            logger.info("==================================================")
            logger.info("Traitement de la requête : '%s'", raw_q)

            # A. Identification automatique via BookMetadataService
            meta = await service.lookup(raw_q)
            if not meta:
                logger.error("❌ Impossible d'identifier l'œuvre pour : %s", raw_q)
                continue

            logger.info("✅ Œuvre identifiée :")
            logger.info("   • Titre officiel : %s", meta.title)
            logger.info("   • Auteur : %s", meta.author)
            logger.info("   • Éditeur : %s", meta.publisher)
            logger.info("   • ISBN-13 : %s", meta.isbn13)
            logger.info("   • Inclus : %s", meta.recommended_includes)
            logger.info("   • Exclus : %s", meta.recommended_excludes)
            logger.info("   • Variantes : %s", meta.recommended_variants)

            # B. Livre dans le catalogue
            book = None
            if meta.isbn13:
                book = await book_repo.get_by_isbn13(meta.isbn13)
            if not book:
                book = await book_repo.create(
                    title=meta.title,
                    normalized_title=normalize_text(meta.title),
                    isbn13=meta.isbn13,
                    author=meta.author,
                    publisher=meta.publisher,
                )
                logger.info("   -> Nouveau livre créé dans le catalogue : %s", book.id)
            else:
                logger.info("   -> Livre déjà présent dans le catalogue : %s", book.id)

            # C. Création ou mise à jour de l'alerte
            stmt_alert = select(Alert).where(
                Alert.user_id == user.id,
                (Alert.isbn == meta.isbn13) | (Alert.name == meta.title),
            )
            alert = (await session.execute(stmt_alert)).scalar_one_or_none()

            vinted_url = (
                f"https://www.vinted.fr/catalog?search_start_type=item_page_isbn&search_text={meta.isbn13}&order=newest_first"
                if meta.isbn13
                else f"https://www.vinted.fr/catalog?search_text={urllib.parse.quote_plus(meta.title)}&order=newest_first"
            )

            if not alert:
                alert = Alert(
                    user_id=user.id,
                    category_id=cat.id,
                    name=meta.title,
                    isbn=meta.isbn13,
                    vinted_search_url=vinted_url,
                    include_keywords=meta.recommended_includes,
                    exclude_keywords=meta.recommended_excludes,
                    variants=meta.recommended_variants,
                    max_item_price_cents=max_cents,
                    accepted_conditions=list(DEFAULT_BD_CONDITIONS),
                    status=AlertStatus.ACTIVE,
                )
                session.add(alert)
                await session.flush()
                logger.info("   -> Alerte créée avec succès (ID: %s)", alert.id)
            else:
                alert.name = meta.title
                alert.isbn = meta.isbn13
                alert.include_keywords = meta.recommended_includes
                alert.exclude_keywords = meta.recommended_excludes
                alert.variants = meta.recommended_variants
                alert.max_item_price_cents = max_cents
                alert.accepted_conditions = list(DEFAULT_BD_CONDITIONS)
                alert.status = AlertStatus.ACTIVE
                alert.vinted_search_url = vinted_url
                logger.info("   -> Alerte existante mise à jour (ID: %s)", alert.id)

            # D. Liaison Alerte <-> Livre
            await book_repo.link_alert_to_book(alert.id, book.id, MatchConfidence.HIGH)

            # E. Préchauffage du marché Chasse-aux-livres
            if meta.isbn13:
                offers = await provider.get_offers(isbn13=meta.isbn13, title=meta.title)
                if offers:
                    for off in offers:
                        await market_repo.save_snapshot(book.id, off)
                    best_offer = ProviderRegistry.select_best_offer(offers)
                    if best_offer:
                        hist_low = (
                            best_offer.raw_payload.get("historical_low_1y_cents")
                            if best_offer.raw_payload
                            else None
                        )
                        await market_repo.update_or_create_cache(
                            book_id=book.id,
                            lowest_delivered_price_cents=best_offer.delivered_price_cents,
                            best_provider_name=best_offer.provider_name,
                            best_source_url=best_offer.source_url,
                            confidence=best_offer.confidence,
                            comparison_status=ComparisonStatus.AVAILABLE,
                            cache_duration_hours=24,
                            historical_low_1y_cents=hist_low,
                        )
                        logger.info(
                            "   -> Cote du marché enregistrée : Livré %d cts (%s), Plus bas 1an : %s cts",
                            best_offer.delivered_price_cents,
                            best_offer.provider_name,
                            hist_low,
                        )
                else:
                    logger.warning("   -> Aucune offre de marché trouvée actuellement pour ISBN %s", meta.isbn13)

            await asyncio.sleep(1.0)

        await session.commit()
        logger.info("==================================================")
        logger.info("Toutes les alertes de test ont été créées et enregistrées avec succès !")


if __name__ == "__main__":
    asyncio.run(run())
