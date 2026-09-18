"""Script d'ajout automatique des alertes à forte liquidité pour l'arbitrage Vinted."""

import asyncio
import logging
import urllib.parse
from sqlalchemy import select

from app.core.config import settings
from app.core.constants import AlertStatus, CategorySlug, DEFAULT_CLOTHES_CONDITIONS, DEFAULT_TECH_CONDITIONS
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.session import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar.add_arbitrage_alerts")

ARBITRAGE_ALERTS = [
    {
        "name": "TI-83 Premium CE",
        "category_slug": CategorySlug.TECH.value,
        "max_price_cents": 2500,  # 25.00 €
        "include_keywords": ["ti 83", "premium ce"],
        "exclude_keywords": ["housse", "coque", "etui", "boite vide", "boite seule", "chargeur seul", "cable seul", "batterie", "batteries", "pile", "piles", "manuel", "guide"],
        "variants": ["ti 83", "ti 83 edition python", "ti83"],
        "conditions": list(DEFAULT_TECH_CONDITIONS),
        "url_search": "TI-83 Premium CE",
    },
    {
        "name": "Nintendo New 3DS XL",
        "category_slug": CategorySlug.TECH.value,
        "max_price_cents": 6000,  # 60.00 €
        "include_keywords": ["new 3ds xl"],
        "exclude_keywords": ["housse", "coque", "etui", "boite vide", "boite seule", "chargeur seul", "stylet seul", "lot de jeux", "jeu seul", "hs", "pour pieces", "ecran casse"],
        "variants": ["new 3ds", "new nintendo 3ds"],
        "conditions": list(DEFAULT_TECH_CONDITIONS),
        "url_search": "New 3DS XL",
    },
    {
        "name": "Sony WH-1000XM4",
        "category_slug": CategorySlug.TECH.value,
        "max_price_cents": 6000,  # 60.00 €
        "include_keywords": ["wh 1000xm4"],
        "exclude_keywords": ["coussinet", "coussinets", "mousse", "etui", "housse", "boite vide", "boite seule", "cable seul", "arceau", "hs", "pour pieces", "marche plus", "panne"],
        "variants": ["wh1000xm4", "1000xm4"],
        "conditions": list(DEFAULT_TECH_CONDITIONS),
        "url_search": "WH-1000XM4",
    },
    {
        "name": "Patagonia Synchilla",
        "category_slug": CategorySlug.CLOTHES.value,
        "max_price_cents": 3500,  # 35.00 €
        "include_keywords": ["patagonia", "synchilla"],
        "exclude_keywords": ["trou", "tache", "abime", "dechirure", "fermeture cassee", "accroc"],
        "variants": ["patagonia snap t"],
        "conditions": list(DEFAULT_CLOTHES_CONDITIONS),
        "url_search": "Patagonia Synchilla",
    },
    {
        "name": "Raspberry Pi 5",
        "category_slug": CategorySlug.TECH.value,
        "max_price_cents": 4500,  # 45.00 €
        "include_keywords": ["raspberry", "pi 5"],
        "exclude_keywords": ["boitier seul", "coque seule", "alimentation seule", "boite vide", "boite seule", "cooler", "ventilateur"],
        "variants": ["rpi 5", "rpi5"],
        "conditions": list(DEFAULT_TECH_CONDITIONS),
        "url_search": "Raspberry Pi 5",
    },
    {
        "name": "Calculatrice NumWorks",
        "category_slug": CategorySlug.TECH.value,
        "max_price_cents": 3000,  # 30.00 €
        "include_keywords": ["numworks"],
        "exclude_keywords": ["coque", "housse", "etui", "cable seul", "boite vide", "boite seule"],
        "variants": ["calculatrice numworks"],
        "conditions": list(DEFAULT_TECH_CONDITIONS),
        "url_search": "NumWorks",
    },
]


async def run():
    async with AsyncSessionLocal() as session:
        alert_repo = AlertRepository(session)

        # Récupérer l'utilisateur
        allowed_ids = settings.TELEGRAM_ALLOWED_USER_IDS
        user_tg_id = allowed_ids[0] if allowed_ids else 6429948805
        stmt_user = select(User).where(User.telegram_user_id == user_tg_id)
        user = (await session.execute(stmt_user)).scalar_one_or_none()
        if not user:
            logger.error("Utilisateur Telegram %s introuvable !", user_tg_id)
            return

        logger.info("Utilisateur trouvé : %s (id=%s)", user.first_name, user.id)

        # Récupérer les catégories
        stmt_cats = select(Category)
        cats = (await session.execute(stmt_cats)).scalars().all()
        cat_map = {c.slug: c for c in cats}

        added_count = 0
        for item in ARBITRAGE_ALERTS:
            cat_slug = item["category_slug"]
            cat = cat_map.get(cat_slug)
            if not cat:
                logger.error("Catégorie '%s' introuvable !", cat_slug)
                continue

            # Vérifier si l'alerte existe déjà
            existing = await alert_repo.find_existing(user.id, title=item["name"])
            if existing:
                logger.info("ℹ️ Alerte déjà existante : '%s' (id=%s, statut=%s) -> Ignorée", item["name"], existing.id, existing.status)
                continue

            vinted_url = f"https://www.vinted.fr/catalog?search_text={urllib.parse.quote_plus(item['url_search'])}&order=newest_first"
            alert = await alert_repo.create(
                user_id=user.id,
                category_id=cat.id,
                name=item["name"],
                isbn=None,
                include_keywords=item["include_keywords"],
                exclude_keywords=item["exclude_keywords"],
                variants=item["variants"],
                max_item_price_cents=item["max_price_cents"],
                accepted_conditions=item["conditions"],
                vinted_search_url=vinted_url,
            )
            logger.info("✅ Alerte créée : '%s' (%s, prix max: %.2f €)", alert.name, cat.label, item["max_price_cents"] / 100)
            added_count += 1

        await session.commit()
        logger.info("==================================================")
        logger.info("Terminé ! %d nouvelles alertes ajoutées avec succès.", added_count)


if __name__ == "__main__":
    asyncio.run(run())
