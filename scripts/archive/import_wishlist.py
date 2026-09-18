"""Script d'importation en masse de la wishlist BD vers les alertes Radar Bot.

Permet de créer automatiquement toutes les alertes de recherche avec leurs filtres :
titre, mots-clés inclus, mots-clés exclus, prix maximum et états acceptés.
"""

import asyncio
import logging
import urllib.parse
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.core.constants import DEFAULT_BD_CONDITIONS, AlertStatus, CategorySlug
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.book_repo import BookRepository
from app.db.session import AsyncSessionLocal
from app.services.book_catalog_service import BookCatalogService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar.import_wishlist")

WISHLIST: list[dict[str, Any]] = [
    {"title": "Blast", "include": ["blast"], "exclude": ["team"], "maxItemPrice": 50},
    {
        "title": "Le Rapport de Brodeck",
        "include": ["brodeck"],
        "exclude": ["philippe claudel roman", "poche"],
        "maxItemPrice": 50,
    },
    {"title": "Watchmen", "include": ["watchmen"], "exclude": [], "maxItemPrice": 50},
    {"title": "Tyler Cross", "include": ["tyler cross"], "exclude": [], "maxItemPrice": 50},
    {
        "title": "Shangri-La — Mathieu Bablet",
        "include": ["shangri la"],
        "exclude": [
            "frontier",
            "manga",
            "zaria",
            "15 ans",
            "oiseau",
            "disparus",
            "zuckoff",
            "shower",
            "route",
            "knox",
        ],
        "maxItemPrice": 30,
    },
    {
        "title": "Le Maître d'armes",
        "include": ["maitre d armes"],
        "exclude": ["roman"],
        "maxItemPrice": 50,
    },
    {
        "title": "1629 — Les naufragés du Jakarta",
        "include": ["1629", "jakarta"],
        "exclude": ["roman"],
        "maxItemPrice": 50,
    },
    {
        "title": "Gunnar le vampire",
        "include": ["gunnar", "vampire"],
        "exclude": ["roman"],
        "maxItemPrice": 50,
    },
    {"title": "Islander", "include": ["islander"], "exclude": ["jeep"], "maxItemPrice": 50},
    {
        "title": "Hoka Hey!",
        "include": ["hoka hey"],
        "exclude": ["chaussure", "running", "sneaker"],
        "maxItemPrice": 50,
    },
    {
        "title": "P.T.S.D. — Guillaume Singelin",
        "include": ["p t s d"],
        "exclude": ["tee shirt", "militaire", "patch", "manga", "radio", "horreur"],
        "maxItemPrice": 50,
    },
    {
        "title": "Monkey Bizness",
        "include": ["monkey", "bizness"],
        "exclude": [],
        "maxItemPrice": 50,
    },
    {
        "title": "Batman Terre Un",
        "include": ["batman", "terre", "integrale"],
        "exclude": ["terre a terre"],
        "maxItemPrice": 50,
    },
    {
        "title": "Batman & Robin — Année Un",
        "include": ["batman", "robin", "annee"],
        "exclude": [],
        "maxItemPrice": 50,
    },
    {"title": "Bellicus", "include": ["bellicus"], "exclude": [], "maxItemPrice": 50},
    {
        "title": "Carcajou",
        "include": ["carcajou"],
        "exclude": [
            "bernard clavel",
            "louve du noirmont",
            "roman",
            "yakari",
            "clavel",
            "vengeance",
        ],
        "maxItemPrice": 50,
    },
    {
        "title": "Frankenstein — Georges Bess / David Sala",
        "include": ["frankenstein"],
        "exclude": ["junji ito", "mary shelley roman", "poche", "père", "manga"],
        "maxItemPrice": 50,
    },
    {
        "title": "The Grocery",
        "include": ["grocery"],
        "exclude": ["sac", "tote bag", "vinyle"],
        "maxItemPrice": 50,
    },
    {
        "title": "La Terre verte",
        "include": ["terre verte"],
        "exclude": ["plante", "jardin", "roman", "pigment"],
        "maxItemPrice": 50,
    },
    {
        "title": "Sangoma",
        "include": ["sangoma"],
        "exclude": ["roman", "veilleurs"],
        "maxItemPrice": 30,
    },
    {
        "title": "Le Château des animaux",
        "include": ["chateau", "animaux"],
        "exclude": ["ferme des animaux roman"],
        "maxItemPrice": 50,
    },
    {
        "title": "Akira - Tome",
        "include": ["akira"],
        "exclude": ["himekawa", "manga", "zelda", "ashimo", "toriyama", "dragon ball", "caca"],
        "maxItemPrice": 50,
    },
    {
        "title": "Mutafukaz' Puta Madre",
        "include": ["puta madre"],
        "exclude": [],
        "maxItemPrice": 50,
    },
    {
        "title": "Beneath the Trees Where Nobody Sees",
        "include": ["beneath the trees"],
        "exclude": ["vo", "english", "anglais"],
        "maxItemPrice": 50,
    },
    {"title": "Slava", "include": ["slava"], "exclude": [], "maxItemPrice": 50},
    {"title": "Silent Jenny", "include": ["silent jenny"], "exclude": [], "maxItemPrice": 50},
    {"title": "RIP", "include": ["rip"], "exclude": ["rest in peace", "funko"], "maxItemPrice": 50},
    {
        "title": "L'Homme sans peur",
        "include": ["homme sans peur"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 50,
    },
    {"title": "Blacksad", "include": ["blacksad"], "exclude": [], "maxItemPrice": 50},
    {
        "title": "Batman — Un long Halloween",
        "include": ["long halloween"],
        "exclude": ["funko"],
        "maxItemPrice": 50,
    },
    {"title": "Un océan d'amour", "include": ["ocean", "amour"], "exclude": [], "maxItemPrice": 50},
    {
        "title": "Spider-Man — La dernière chasse de Kraven",
        "include": ["kraven"],
        "exclude": ["hot toys"],
        "maxItemPrice": 50,
    },
    {
        "title": "Le Château des étoiles — Intégrale",
        "include": ["chateau", "etoiles"],
        "exclude": ["disney", "lego"],
        "maxItemPrice": 30,
    },
    {
        "title": "La guerre éternelle — Intégrale",
        "include": ["guerre eternelle"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 30,
    },
    {
        "title": "The Nice House by the Sea",
        "include": ["nice", "sea"],
        "exclude": [],
        "maxItemPrice": 50,
    },
    {
        "title": "Amer béton — Intégrale",
        "include": ["amer", "beton"],
        "exclude": [],
        "maxItemPrice": 30,
    },
    {
        "title": "Le Combat Ordinaire",
        "include": ["combat", "ordinaire"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 50,
    },
    {"title": "Loba Loca", "include": ["loba loca"], "exclude": [], "maxItemPrice": 50},
    {
        "title": "Urban — Intégrale",
        "include": ["urban", "integrale"],
        "exclude": ["urban comics", "urban decay", "comics", "comcis", "dc"],
        "maxItemPrice": 30,
    },
    {
        "title": "Les Chants du Cygne Noir",
        "include": ["chants", "cygne"],
        "exclude": ["roman", "ballet"],
        "maxItemPrice": 50,
    },
    {"title": "Thrillerville", "include": ["thrillerville"], "exclude": [], "maxItemPrice": 50},
    {"title": "Do a Powerbomb", "include": ["power bomb"], "exclude": [], "maxItemPrice": 50},
    {
        "title": "Soli Deo Gloria",
        "include": ["soli deo gloria"],
        "exclude": ["partition"],
        "maxItemPrice": 50,
    },
    {
        "title": "Terre ou Lune",
        "include": ["terre ou lune"],
        "exclude": ["jules", "verne"],
        "maxItemPrice": 50,
    },
    {
        "title": "Tout seul — Chabouté",
        "include": ["tout seul"],
        "exclude": ["roman", "poche", "vendu tout seul", "tome tout seul"],
        "maxItemPrice": 50,
    },
    {
        "title": "Plus loin qu'ailleurs",
        "include": ["plus loin qu ailleurs"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 50,
    },
    {"title": "The Junction", "include": ["junction"], "exclude": ["lego"], "maxItemPrice": 50},
    {
        "title": "Les montagnes hallucinées",
        "include": ["montagnes", "hallucinees"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 50,
    },
    {
        "title": "L'appel de Cthulhu",
        "include": ["appel de cthulhu"],
        "exclude": ["poche", "roman", "cauchemars", "minimaki", "howard", "H.P"],
        "maxItemPrice": 50,
    },
    {
        "title": "Hélène de Wyndhorn",
        "include": ["wyndhorn"],
        "exclude": ["roman"],
        "maxItemPrice": 50,
    },
    {
        "title": "Notre-Dame de Paris — Bess",
        "include": ["notre dame de paris"],
        "exclude": [
            "disney",
            "roman",
            "hugo",
            "poche",
            "magnet",
            "bossu",
            "victor",
            "grande",
            "aventure",
            "bovary",
            "indispensables",
            "gargouilles",
            "aventures",
            "fantastiques",
            "sacre",
        ],
        "maxItemPrice": 50,
    },
    {
        "title": "Dracula — Bess",
        "include": ["dracula"],
        "exclude": [
            "roman",
            "poche",
            "manga",
            "comics",
            "prince",
            "pascal",
            "russell",
            "horror",
            "valentino",
            "hermann",
            "traces",
            "vlad",
            "immortel",
            "kowalski",
            "hippolyte",
            "monstres",
            "fernando",
            "croci",
            "jekyll",
        ],
        "maxItemPrice": 50,
    },
    {
        "title": "Perpendiculaire au soleil",
        "include": ["perpendiculaire"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 50,
    },
    {
        "title": "Les travailleurs de la mer",
        "include": ["travailleurs de la mer"],
        "exclude": ["hugo", "roman", "poche"],
        "maxItemPrice": 50,
    },
    {
        "title": "La Tête de mort venue de Suède",
        "include": ["venue de suede"],
        "exclude": ["halloween", "decoration"],
        "maxItemPrice": 50,
    },
    {
        "title": "Les Gorilles du général",
        "include": ["gorilles", "general"],
        "exclude": ["dvd"],
        "maxItemPrice": 50,
    },
    {
        "title": "Watership Down",
        "include": ["watership"],
        "exclude": ["peluche"],
        "maxItemPrice": 50,
    },
    {
        "title": "1984 — Xavier Coste",
        "include": ["1984"],
        "exclude": [
            "orwell roman",
            "poche",
            "casse",
            "barbe",
            "orwell",
            "tome",
            "balade",
            "monde",
            "cartes",
            "nicollet",
            "valhardi",
        ],
        "maxItemPrice": 50,
    },
    {
        "title": "Pulp — Ed Brubaker",
        "include": ["pulp", "brubaker"],
        "exclude": ["magazine", "roman"],
        "maxItemPrice": 50,
    },
    {
        "title": "Le Poids des héros",
        "include": ["poids", "heros"],
        "exclude": [],
        "maxItemPrice": 50,
    },
    {
        "title": "T'Zée — Une tragédie africaine",
        "include": ["t zee"],
        "exclude": ["lucky luke"],
        "maxItemPrice": 50,
    },
    {
        "title": "Batman — Amère victoire",
        "include": ["amere", "victoire"],
        "exclude": [],
        "maxItemPrice": 50,
    },
    {
        "title": "Batman — Dark Victory",
        "include": ["dark victory"],
        "exclude": ["dark knight"],
        "maxItemPrice": 50,
    },
    {
        "title": "L'Orfèvre — Lozes",
        "include": ["orfevre", "lozes"],
        "exclude": ["bijou", "bague", "collier", "montre"],
        "maxItemPrice": 50,
    },
    {
        "title": "Le Loup des Mers — Riff Reb's",
        "include": ["loup des mers"],
        "exclude": ["london roman", "poche", "audiobook"],
        "maxItemPrice": 50,
    },
    {
        "title": "Hommes à la mer",
        "include": ["hommes a la mer"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 50,
    },
    {
        "title": "Train de nuit dans la Voie lactée",
        "include": ["voie lactee"],
        "exclude": ["puzzle", "manga"],
        "maxItemPrice": 50,
    },
    {
        "title": "Journal de 1985 — Xavier Coste",
        "include": ["journal de 1985"],
        "exclude": ["agenda", "calendrier", "cahier", "roman", "poche"],
        "maxItemPrice": 50,
    },
    {
        "title": "Rubrique-à-Brac — Gotlib",
        "include": ["rubrique", "brac"],
        "exclude": ["Gallery"],
        "maxItemPrice": 50,
    },
    {
        "title": "Sharaz-De — Sergio Toppi",
        "include": ["sharaz"],
        "exclude": ["dvd"],
        "maxItemPrice": 50,
    },
    {"title": "Sergio Toppi", "include": ["toppi"], "exclude": ["dvd"], "maxItemPrice": 50},
    {
        "title": "Le Collectionneur — Sergio Toppi",
        "include": ["collectionneur"],
        "exclude": ["pour collectionneur", "etat collectionneur"],
        "maxItemPrice": 50,
    },
    {
        "title": "La Favorite — Matthias Lehmann",
        "include": ["favorite"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 50,
    },
    {
        "title": "Le Roi Méduse — Brecht Evens",
        "include": ["roi meduse"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 25,
    },
    {
        "title": "2001 Nights Stories — Yukinobu Hoshino",
        "include": ["2001 nights"],
        "exclude": ["odyssee"],
        "maxItemPrice": 30,
    },
    {
        "title": "Demon — Jason Shiga",
        "include": ["demon", "shiga"],
        "exclude": ["demon slayer", "manga", "roman"],
        "maxItemPrice": 25,
    },
    {
        "title": "Minor Arcana — Jeff Lemire",
        "include": ["minor", "arcana"],
        "exclude": ["tarot", "cartes", "oracle"],
        "maxItemPrice": 30,
    },
    {"title": "LowReader", "include": ["lowreader"], "exclude": ["magazine"], "maxItemPrice": 50},
    {
        "title": "L'Enfer de Dante — Paul et Gaëtan Brizzi",
        "include": ["enfer de dante"],
        "exclude": ["divine comedie roman", "poche"],
        "maxItemPrice": 25,
    },
    {
        "title": "Le Monde sans fin — Christophe Blain / Jean-Marc Jancovici",
        "include": ["monde sans fin"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 25,
    },
    {
        "title": "Jesse James — Dobbs / Christophe Regnault",
        "include": ["jesse james"],
        "exclude": ["lucky luke"],
        "maxItemPrice": 50,
    },
    {
        "title": "Raptor — Dave McKean",
        "include": ["raptor"],
        "exclude": ["jurassic", "dinosaure", "dino"],
        "maxItemPrice": 50,
    },
    {
        "title": "La Cage aux cons — Robin Recht / Matthieu Angotti",
        "include": ["cage aux cons"],
        "exclude": ["cage aux folles"],
        "maxItemPrice": 50,
    },
    {
        "title": "20th Century Boys — Naoki Urasawa",
        "include": ["20th", "century", "boys"],
        "exclude": ["21st century boys"],
        "maxItemPrice": 50,
    },
    {
        "title": "L'odyssée d'Hakim — Fabien Toulmé",
        "include": ["odyssee", "hakim"],
        "exclude": [],
        "maxItemPrice": 50,
    },
    {
        "title": "Ulysse & Cyrano — Antoine Cristau / Xavier Dorison / Stéphane Servain",
        "include": ["ulysse", "cyrano"],
        "exclude": ["homere", "odyssee", "roman", "poche"],
        "maxItemPrice": 30,
    },
    {
        "title": "Persepolis — Marjane Satrapi",
        "include": ["persepolis"],
        "exclude": [],
        "maxItemPrice": 50,
    },
    {
        "title": "Les Voyages d'Ulysse — Emmanuel Lepage / René Follet / Sophie Michel",
        "include": ["voyages", "ulysse"],
        "exclude": ["homere", "odyssee", "roman", "poche"],
        "maxItemPrice": 50,
    },
    {
        "title": "Dans la tête de Sherlock Holmes — Benoît Dahan / Cyril Lieron",
        "include": ["tete", "sherlock", "holmes"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 25,
    },
    {
        "title": "These Savage Shores — Ram V / Sumit Kumar",
        "include": ["savage shores"],
        "exclude": [],
        "maxItemPrice": 50,
    },
    {
        "title": (
            "Cauchon… ou l'homme qui tua Jeanne d'Arc — Xavier Dorison / Louis-David Delahaye /"
            " Joël Parnotte"
        ),
        "include": ["cauchon"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 30,
    },
    {
        "title": "The Junction",
        "include": ["the junction"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 50,
    },
    {
        "title": "La langue des vipères",
        "include": ["langue des viperes"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 50,
    },
    {
        "title": "Dead Train",
        "include": ["dead train"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 50,
    },
    {
        "title": "Winter Road",
        "include": ["winter road"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 50,
    },
    {"title": "Junk", "include": ["junk"], "exclude": ["roman", "poche"], "maxItemPrice": 50},
    {
        "title": "Adrastée",
        "include": ["adrasté"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 50,
    },
    {
        "title": "Âme augmentée",
        "include": ["ame augmentée"],
        "exclude": ["roman", "poche"],
        "maxItemPrice": 50,
    },
    {
        "title": "La Route — Manu Larcenet",
        "include": ["la route"],
        "exclude": [
            "roman",
            "cormac mccarthy roman",
            "damnés",
            "harlem",
            "eldorado",
            "sur la route",
            "simpson",
        ],
        "maxItemPrice": 18,
    },
    {
        "title": "Les Indes fourbes",
        "include": ["indes fourbes"],
        "exclude": ["poster", "affiche", "ex libris", "marque page"],
        "maxItemPrice": 50,
    },
    {
        "title": "Carbone & Silicium — Mathieu Bablet",
        "include": ["carbone silicium"],
        "exclude": ["or noir"],
        "maxItemPrice": 16,
    },
    {
        "title": "La Bibliomule de Cordoue",
        "include": ["bibliomule cordoue"],
        "exclude": [],
        "maxItemPrice": 22,
    },
]


async def import_wishlist() -> None:
    """Importe toutes les alertes de la wishlist en base de données."""
    async with AsyncSessionLocal() as session:
        # 1. Récupérer l'utilisateur Telegram
        allowed_ids = settings.TELEGRAM_ALLOWED_USER_IDS
        stmt_user = select(User).where(User.telegram_user_id.in_(allowed_ids))
        user = (await session.execute(stmt_user)).scalars().first()

        if not user:
            # Fallback : premier utilisateur inscrit
            stmt_first_user = select(User).order_by(User.created_at.asc())
            user = (await session.execute(stmt_first_user)).scalars().first()

        if not user:
            logger.error(
                "Aucun utilisateur trouvé en base de données ! Envoyez /start au bot d'abord."
            )
            return

        logger.info(
            "Utilisateur identifié : %s (Telegram ID: %s, UUID: %s)",
            user.username or "Anonyme",
            user.telegram_user_id,
            user.id,
        )

        # 2. Récupérer la catégorie BD & Comics
        stmt_cat = select(Category).where(Category.slug == CategorySlug.BD_COMICS.value)
        category = (await session.execute(stmt_cat)).scalar_one_or_none()
        if not category:
            logger.error("Catégorie 'bd_comics' introuvable !")
            return

        catalog_service = BookCatalogService(session)
        book_repo = BookRepository(session)

        # 3. Charger les alertes existantes pour cet utilisateur
        stmt_existing = select(Alert).where(
            Alert.user_id == user.id,
            Alert.status != AlertStatus.DELETED,
        )
        existing_alerts = {
            a.name: a for a in (await session.execute(stmt_existing)).scalars().all()
        }

        created_count = 0
        updated_count = 0

        for item in WISHLIST:
            title = item["title"]
            include_kws = item["include"]
            exclude_kws = item["exclude"]
            max_cents = int(item["maxItemPrice"] * 100)

            # Construction de l'URL Vinted filtrée catégorie BD, français, états récents
            search_query = urllib.parse.quote_plus(" ".join(include_kws))
            vinted_url = (
                f"https://www.vinted.fr/catalog?search_text={search_query}"
                f"&catalog[]=5425&status_ids[]=1&status_ids[]=2&status_ids[]=6"
                f"&language_book_ids[]=6436&search_id=2946920741&order=newest_first"
            )

            if title in existing_alerts:
                alert = existing_alerts[title]
                alert.include_keywords = include_kws
                alert.exclude_keywords = exclude_kws
                alert.max_item_price_cents = max_cents
                alert.vinted_search_url = vinted_url
                alert.status = AlertStatus.ACTIVE
                updated_count += 1
            else:
                alert = Alert(
                    user_id=user.id,
                    category_id=category.id,
                    name=title,
                    include_keywords=include_kws,
                    exclude_keywords=exclude_kws,
                    variants=[],
                    max_item_price_cents=max_cents,
                    accepted_conditions=DEFAULT_BD_CONDITIONS,
                    vinted_search_url=vinted_url,
                    status=AlertStatus.ACTIVE,
                )
                session.add(alert)
                await session.flush()

                # Recherche d'un livre existant dans le catalogue
                book, _ = await catalog_service.identify_book(title=title)
                if not book:
                    for kw in include_kws:
                        book, _ = await catalog_service.identify_book(title=kw)
                        if book:
                            break
                if book:
                    await book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)

                created_count += 1

        await session.commit()
        logger.info(
            "✅ Importation terminée : %d alertes créées, %d mises à jour "
            "(Total: %d alertes actives).",
            created_count,
            updated_count,
            created_count + updated_count,
        )


if __name__ == "__main__":
    asyncio.run(import_wishlist())
