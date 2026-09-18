"""Script d'ajout automatique des 5 meilleures alertes Tier 1 Nintendo Switch."""

import asyncio
import logging
import urllib.parse
from sqlalchemy import select

from app.core.config import settings
from app.core.constants import AlertStatus, CategorySlug, DEFAULT_GAMING_CONDITIONS
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.session import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar.add_switch_tier1_alerts")

SWITCH_TIER1_ALERTS = [
    {
        "name": "Mario Kart 8 Deluxe (Switch)",
        "category_slug": CategorySlug.GAMING.value,
        "max_price_cents": 1800,  # 18.00 € (se revend 32-36 €)
        "include_keywords": ["mario kart 8 deluxe"],
        "exclude_keywords": [
            "boite vide", "boite seule", "boitier vide", "boitier seul",
            "sans jeu", "sans cartouche", "volant", "volants",
            "housse", "circuit", "home circuit", "casse", "hs", "figurine"
        ],
        "variants": ["mario kart 8", "mario kart deluxe", "mk8 deluxe", "mk8d"],
        "conditions": list(DEFAULT_GAMING_CONDITIONS),
        "url_search": "Mario Kart 8 Deluxe Switch",
    },
    {
        "name": "Super Smash Bros. Ultimate (Switch)",
        "category_slug": CategorySlug.GAMING.value,
        "max_price_cents": 2000,  # 20.00 € (se revend 35-40 €)
        "include_keywords": ["smash bros ultimate"],
        "exclude_keywords": [
            "boite vide", "boite seule", "boitier vide", "boitier seul",
            "sans jeu", "sans cartouche", "amiibo", "manette", "figurine",
            "housse", "casse", "hs"
        ],
        "variants": ["smash bros", "super smash bros", "ssbu"],
        "conditions": list(DEFAULT_GAMING_CONDITIONS),
        "url_search": "Super Smash Bros Ultimate Switch",
    },
    {
        "name": "Zelda: Breath of the Wild (Switch)",
        "category_slug": CategorySlug.GAMING.value,
        "max_price_cents": 2000,  # 20.00 € (se revend 30-35 €)
        "include_keywords": ["breath of the wild"],
        "exclude_keywords": [
            "boite vide", "boite seule", "boitier vide", "boitier seul",
            "sans jeu", "sans cartouche", "guide", "livre", "amiibo", "figurine",
            "wii u", "wiiu", "housse", "casse", "hs"
        ],
        "variants": ["botw", "zelda breath of the wild", "zelda botw"],
        "conditions": list(DEFAULT_GAMING_CONDITIONS),
        "url_search": "Zelda Breath of the Wild Switch",
    },
    {
        "name": "Super Mario Odyssey (Switch)",
        "category_slug": CategorySlug.GAMING.value,
        "max_price_cents": 1800,  # 18.00 € (se revend 30-35 €)
        "include_keywords": ["mario odyssey"],
        "exclude_keywords": [
            "boite vide", "boite seule", "boitier vide", "boitier seul",
            "sans jeu", "sans cartouche", "guide", "livre", "figurine",
            "housse", "casse", "hs", "peluche"
        ],
        "variants": ["super mario odyssey"],
        "conditions": list(DEFAULT_GAMING_CONDITIONS),
        "url_search": "Super Mario Odyssey Switch",
    },
    {
        "name": "Animal Crossing: New Horizons (Switch)",
        "category_slug": CategorySlug.GAMING.value,
        "max_price_cents": 1800,  # 18.00 € (se revend 28-32 €)
        "include_keywords": ["animal crossing", "new horizons"],
        "exclude_keywords": [
            "boite vide", "boite seule", "boitier vide", "boitier seul",
            "sans jeu", "sans cartouche", "carte", "cartes", "amiibo",
            "figurine", "housse", "casse", "hs", "pochette"
        ],
        "variants": ["animal crossing new horizons", "acnh"],
        "conditions": list(DEFAULT_GAMING_CONDITIONS),
        "url_search": "Animal Crossing New Horizons Switch",
    },
]


async def run():
    async with AsyncSessionLocal() as session:
        alert_repo = AlertRepository(session)

        # 1. Récupérer l'utilisateur
        allowed_ids = settings.TELEGRAM_ALLOWED_USER_IDS
        user_tg_id = allowed_ids[0] if allowed_ids else 6429948805
        stmt_user = select(User).where(User.telegram_user_id == user_tg_id)
        user = (await session.execute(stmt_user)).scalar_one_or_none()
        if not user:
            logger.error("Utilisateur Telegram %s introuvable !", user_tg_id)
            return

        logger.info("Utilisateur trouvé : %s (id=%s)", user.first_name, user.id)

        # 2. Récupérer la catégorie tech
        stmt_cat = select(Category).where(Category.slug == CategorySlug.TECH.value)
        cat = (await session.execute(stmt_cat)).scalar_one_or_none()
        if not cat:
            logger.error("Catégorie 'tech' introuvable !")
            return

        added_count = 0
        for item in SWITCH_TIER1_ALERTS:
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
            logger.info(
                "✅ Alerte créée : '%s' (%s, prix max: %.2f €)",
                alert.name, cat.label, item["max_price_cents"] / 100
            )
            added_count += 1

        await session.commit()
        logger.info("==================================================")
        logger.info("Terminé ! %d alertes Switch Tier 1 ajoutées avec succès.", added_count)


if __name__ == "__main__":
    asyncio.run(run())
