"""Script d'ajout et synchronisation des 12 nouvelles alertes BD + mise à jour du catalogue et cache."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from sqlalchemy import select

from app.core.config import settings
from app.core.constants import AlertStatus, CategorySlug, MatchConfidence
from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.book_repo import BookRepository
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.db.session import AsyncSessionLocal
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
from app.services.matcher_service import normalize_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar.add_bd")

NEW_BD_LIST = [
    {
        "name": "Ion Mud",
        "author": "Amaury Bündgen",
        "publisher": "Casterman",
        "isbn": "9782203202931",
        "include_keywords": ["ion mud", "ivan mud"],
        "exclude_keywords": ["roman", "poche", "anglais", "english"],
    },
    {
        "name": "Nymphéas noirs",
        "author": "Fred Duval / Didier Cassegrain / Michel Bussi",
        "publisher": "Dupuis",
        "isbn": "9782800173504",
        "include_keywords": ["nympheas noirs", "ninfeas noir", "nymphea noir"],
        "exclude_keywords": ["roman", "poche", "livre de poche", "audio", "bussi roman"],
    },
    {
        "name": "Je mourrai pas gibier",
        "author": "Alfred / Guillaume Guéraud",
        "publisher": "Delcourt",
        "isbn": "9782756013138",
        "include_keywords": ["mourrai pas gibier", "mourrais pas gibier", "je mourrai pas gibier"],
        "exclude_keywords": ["roman", "poche", "gueraud roman"],
    },
    {
        "name": "La Bête — Chabouté",
        "author": "Christophe Chabouté",
        "publisher": "Vents d'Ouest",
        "isbn": "9782749300054",
        "include_keywords": ["la bete", "chaboute"],
        "exclude_keywords": ["frank pe", "zidrou", "marsupilami", "disney", "roman"],
    },
    {
        "name": "Tout seul — Chabouté",
        "author": "Christophe Chabouté",
        "publisher": "Vents d'Ouest",
        "isbn": "9782749304298",
        "include_keywords": ["tout seul", "chaboute"],
        "exclude_keywords": ["roman", "dvd", "jeu"],
    },
    {
        "name": "Le Poids des héros",
        "author": "David Sala",
        "publisher": "Casterman",
        "isbn": "9782203215764",
        "include_keywords": ["poids des heros"],
        "exclude_keywords": ["roman", "affiche", "poster"],
    },
    {
        "name": "La Nuit — Philippe Druillet",
        "author": "Philippe Druillet",
        "publisher": "Glénat",
        "isbn": "9782344000205",
        "include_keywords": ["la nuit", "druillet"],
        "exclude_keywords": ["wiesel", "roman", "poche", "dvd"],
    },
    {
        "name": "Hard Boiled",
        "author": "Frank Miller / Geof Darrow",
        "publisher": "Delcourt",
        "isbn": "9782756030081",
        "include_keywords": ["hard boiled", "arde boiled", "hardboiled"],
        "exclude_keywords": ["dvd", "film", "chow yun fat", "john woo"],
    },
    {
        "name": "Jimmy Corrigan",
        "author": "Chris Ware",
        "publisher": "Delcourt",
        "isbn": "9782840558057",
        "include_keywords": ["jimmy corrigan"],
        "exclude_keywords": ["anglais", "english", "pantheon"],
    },
    {
        "name": "La Trilogie Nikopol",
        "author": "Enki Bilal",
        "publisher": "Casterman",
        "isbn": "9782203158450",
        "include_keywords": ["trilogie nikopol", "trilogie nicopol", "nikopol integrale"],
        "exclude_keywords": ["dvd", "immortel", "jeu", "figurine"],
    },
    {
        "name": "Le Géant chagrin",
        "author": "David Sala / Carole Martinez",
        "publisher": "Casterman",
        "isbn": "9782203157224",
        "include_keywords": ["geant chagrin"],
        "exclude_keywords": ["roman", "poche"],
    },
    {
        "name": "Hommes à la mer",
        "author": "Riff Reb's",
        "publisher": "Soleil",
        "isbn": "9782302043114",
        "include_keywords": ["hommes a la mer", "homme a la mer"],
        "exclude_keywords": ["roman", "poche"],
    },
]

CONDITIONS = ["Neuf avec étiquette", "Neuf sans étiquette", "Très bon état"]
MAX_PRICE_CENTS = 5000  # 50 €


async def run_batch_add():
    provider = ChasseAuxLivresProvider(timeout_seconds=12.0)
    
    async with AsyncSessionLocal() as session:
        book_repo = BookRepository(session)
        market_repo = MarketPriceRepository(session)

        # 1. Récupérer l'utilisateur
        allowed_ids = settings.TELEGRAM_ALLOWED_USER_IDS
        user_tg_id = allowed_ids[0] if allowed_ids else 6429948805
        stmt_user = select(User).where(User.telegram_user_id == user_tg_id)
        user = (await session.execute(stmt_user)).scalar_one_or_none()
        if not user:
            logger.error("Utilisateur Telegram %s introuvable en base !", user_tg_id)
            return

        # 2. Récupérer la catégorie BD
        stmt_cat = select(Category).where(Category.slug == CategorySlug.BD_COMICS.value)
        cat = (await session.execute(stmt_cat)).scalar_one_or_none()
        if not cat:
            logger.error("Catégorie bd_comics introuvable en base !")
            return

        # 3. Traiter chaque BD
        for item in NEW_BD_LIST:
            title = item["name"]
            isbn = item["isbn"]
            author = item["author"]
            publisher = item["publisher"]
            includes = item["include_keywords"]
            excludes = item["exclude_keywords"]

            logger.info("=== Traitement : %s (ISBN: %s) ===", title, isbn)

            # A. Recherche ou création du livre dans le catalogue
            book = await book_repo.get_by_isbn13(isbn)
            if not book:
                book = await book_repo.create(
                    title=title,
                    normalized_title=normalize_text(title),
                    isbn13=isbn,
                    author=author,
                    publisher=publisher,
                )
                logger.info("  -> Livre créé dans le catalogue : %s", book.id)
            else:
                logger.info("  -> Livre existant dans le catalogue : %s", book.id)

            # B. Recherche ou création de l'alerte
            stmt_alert = select(Alert).where(
                Alert.user_id == user.id,
                (Alert.isbn == isbn) | (Alert.name == title),
            )
            alert = (await session.execute(stmt_alert)).scalar_one_or_none()

            vinted_url = f"https://www.vinted.fr/catalog?search_start_type=item_page_isbn&search_text={isbn}&order=newest_first"

            if not alert:
                alert = Alert(
                    user_id=user.id,
                    category_id=cat.id,
                    name=title,
                    isbn=isbn,
                    vinted_search_url=vinted_url,
                    include_keywords=includes,
                    exclude_keywords=excludes,
                    max_item_price_cents=MAX_PRICE_CENTS,
                    accepted_conditions=CONDITIONS,
                    status=AlertStatus.ACTIVE,
                )
                session.add(alert)
                await session.flush()
                logger.info("  -> Alerte créée : %s (ID: %s)", alert.name, alert.id)
            else:
                alert.name = title
                alert.isbn = isbn
                alert.include_keywords = includes
                alert.exclude_keywords = excludes
                alert.max_item_price_cents = MAX_PRICE_CENTS
                alert.accepted_conditions = CONDITIONS
                alert.status = AlertStatus.ACTIVE
                alert.vinted_search_url = vinted_url
                logger.info("  -> Alerte mise à jour : %s (ID: %s)", alert.name, alert.id)

            # C. Liaison Alerte <-> Livre
            await book_repo.link_alert_to_book(alert.id, book.id, MatchConfidence.HIGH)

            # D. Warmup du cache de prix Chasse-aux-livres
            offers = await provider.get_offers(isbn13=isbn, title=title)
            if offers:
                for offer in offers:
                    await market_repo.save_snapshot(book.id, offer)

                from app.providers.registry import ProviderRegistry
                from app.core.constants import ComparisonStatus

                best_offer = ProviderRegistry.select_best_offer(offers)
                if best_offer:
                    hist_low = best_offer.raw_payload.get("historical_low_1y_cents") if best_offer.raw_payload else None
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
                    logger.info("  -> Cache marché rempli (Livré: %d cts, 1an: %s cts).", best_offer.delivered_price_cents, hist_low)
            else:
                logger.warning("  -> Aucune offre trouvée pour ISBN %s sur Chasse-aux-livres.", isbn)

            await asyncio.sleep(1.0)

        # Mettre aussi à jour Carcajou et Watchmen
        stmt_carcajou = select(Alert).where(Alert.name.ilike("%carcajou%"))
        carcajou = (await session.execute(stmt_carcajou)).scalar_one_or_none()
        if carcajou:
            carcajou.exclude_keywords = ["bernard clavel", "louve du noirmont", "roman", "yakari", "clavel", "vengeance"]
            carcajou.max_item_price_cents = MAX_PRICE_CENTS
            carcajou.accepted_conditions = CONDITIONS
            logger.info("Alerte Carcajou consolidée.")

        stmt_watchmen = select(Alert).where(Alert.name.ilike("%watchmen%"))
        watchmen = (await session.execute(stmt_watchmen)).scalar_one_or_none()
        if watchmen:
            watchmen.exclude_keywords = ["anglais", "english", "single issue", "dvd", "blu ray"]
            watchmen.max_item_price_cents = MAX_PRICE_CENTS
            watchmen.accepted_conditions = CONDITIONS
            logger.info("Alerte Watchmen consolidée.")

        await session.commit()
        logger.info("Tous les ajouts et mises à jour ont été enregistrés avec succès !")

if __name__ == "__main__":
    asyncio.run(run_batch_add())
