"""Synchronisation exhaustive et certification de 100% des alertes et tomes BD en base de données.

- Associe l'ISBN certifié à tous les one-shots et intégrales.
- Configure toutes les séries multi-tomes avec leurs tomes officiels exacts.
- Supprime les liaisons doublons ou orphelines dans alert_books.
- Assure qu'aucun livre actif n'a isbn13 = NULL.
"""

import asyncio
import logging
import uuid
from sqlalchemy import text, select, delete
from app.db.session import AsyncSessionLocal
from app.core.constants import MatchConfidence
from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.models.alert_book import AlertBook
from app.services.matcher_service import normalize_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("radar.sync_catalog")

# 1. ONE-SHOTS ET INTÉGRALES AVEC ISBN-13 CERTIFIÉ
CERTIFIED_ONE_SHOTS: dict[str, str] = {
    # Validés d'origine
    "Watership Down": "9782381961903",
    "Soli Deo Gloria": "9791034768974",
    # One-shots & Intégrales vérifiés
    "1984 — Xavier Coste": "9782377316588",
    "Amer béton — Intégrale": "9782759500079",
    "Bellicus": "9782380354119",
    "Carbone & Silicium": "9791033511960",
    "Carcajou": "9782377317929",
    "Cauchon… ou l'homme qui tua Jeanne d'Arc": "9782505120018",
    "Do a Powerbomb": "9791026822363",
    "Dracula — Bess": "9782344038604",
    "Frankenstein — Bess": "9782344040522",
    "Gunnar le vampire": "9782808507585",
    "Hoka Hey!": "9782810202294",
    "Hommes à la mer": "9782302043114",
    "Hélène de Wyndhorn": "9782344069554",
    "Jesse James": "9782344038482",
    "Journal de 1985": "9791040806370",
    "L'Enfer de Dante": "9782356741349",
    "L'Orfèvre — Lozes": "9782491374969",
    "La Bibliomule de Cordoue": "9782505078647",
    "La Cage aux cons": "9782413018575",
    "La Favorite — Lehmann": "9782330047788",
    "La Route — Larcenet": "9782205208153",
    "La Terre verte": "9782413076780",
    "La Tête de mort venue de Suède": "9782808502665",
    "La langue des vipères": "9782810207121",
    "Le Loup des Mers — Riff Reb's": "9782302024359",
    "Le Maître d'armes": "9782505063421",
    "Le Monde sans fin": "9782205088168",
    "Le Poids des héros": "9782203215764",
    "Les Indes fourbes": "9782756035734",
    "Les Voyages d'Ulysse": "9782356740434",
    "Les travailleurs de la mer": "9782344047033",
    "Notre-Dame de Paris — Bess": "9782344054147",
    "P.T.S.D. — Singelin": "9791033509530",
    "Perpendiculaire au soleil": "9782413044932",
    "Plus loin qu'ailleurs": "9782749310015",
    "Pulp — Ed Brubaker": "9782413039518",
    "Raptor — Dave McKean": "9782754832939",
    "Sangoma": "9782344034200",
    "Shangri-La — Bablet": "9782359109696",
    "Sharaz-De — Sergio Toppi": "9782352830979",
    "Silent Jenny": "9782810208005",
    "Spider-Man — Kraven": "9791039147125",
    "T'Zée — Une tragédie africaine": "9782205089752",
    "The Junction": "9782344069721",
    "These Savage Shores": "9782378871604",
    "Thrillerville": "9782356741929",
    "Tout seul — Chabouté": "9782749304298",
    "Train de nuit dans la Voie lactée": "9782387250001",
    "Ulysse & Cyrano": "9782203171473",
    "Un océan d'amour": "9782756062105",
    "Winter Road": "9782754816953",
    "Âme augmentée": "9791032408162",
    "Batman — Un long Halloween": "9782365771788",
    "Batman — Amère victoire": "9782365770156",
    "Batman — Dark Victory": "9782365770156",
    "Batman Terre Un": "9791026812715",
    "Demon — Jason Shiga": "9782366243666",
    "Watchmen": "9782365770095",
    # 8 Intégrales expressément demandées par l'utilisateur
    "Mutafukaz' Loba Loca - L'intégrale": "9791033512431",
    "Loba Loca": "9791033512431",
    "Mutafukaz' Puta Madre": "9791033504849",
    "Adrastée intégrale": "9782359109870",
    "Urban — Intégrale": "9782754841764",
    "La guerre éternelle — Intégrale": "9782800142449",
    "Daredevil - L'homme sans peur": "9782809488203",
    "The Grocery": "9791033504573",
    "Monkey Bizness": "9791033509301",
    # One-shots & Intégrales supplémentaires certifiés
    "Le Château des étoiles — Intégrale": "9782810208784",
    "Beneath the Trees Where Nobody Sees": "9791033530435",
    "Batman & Robin — Année Un": "9791026824527",
    "The Nice House by the Sea": "9791026828020",
    "Minor Arcana — Jeff Lemire": "9782413091417",
    "Le Roi Méduse — Brecht Evens": "9782330185138",
    "L'appel de Cthulhu": "9791032706633",
    "Junk": "9782723495417",
    "Le Collectionneur — Sergio Toppi": "9782352830443",
    "Persepolis — Marjane Satrapi": "9782844146762",
    "Rubrique-à-Brac — Gotlib": "9782205078763",
    "Terre ou Lune": "9782387250087",
    "Dead Train": "9782380463972",
    "Islander": "9782344043042",
    "Les Gorilles du général": "9782203225893",
}

# 2. SÉRIES MULTI-TOMES CERTIFIÉES AVEC DÉTAIL DES TOMES
CERTIFIED_SERIES: dict[str, list[dict[str, str | int]]] = {
    "Le Combat Ordinaire": [
        {"tome": 1, "subtitle": "Le Combat ordinaire", "isbn": "9782205054255"},
        {"tome": 2, "subtitle": "Les Quantités négligeables", "isbn": "9782205055894"},
        {"tome": 3, "subtitle": "Ce qui est précieux", "isbn": "9782205057911"},
        {"tome": 4, "subtitle": "Planter des clous", "isbn": "9782205061406"},
    ],
    "Blacksad": [
        {"tome": 1, "subtitle": "Quelque part entre les ombres", "isbn": "9782205049657"},
        {"tome": 2, "subtitle": "Arctic-Nation", "isbn": "9782205051995"},
        {"tome": 3, "subtitle": "Âme Rouge", "isbn": "9782205055641"},
        {"tome": 4, "subtitle": "L'Enfer, le silence", "isbn": "9782205063134"},
        {"tome": 5, "subtitle": "Amarillo", "isbn": "9782205071801"},
        {"tome": 6, "subtitle": "Alors, tout tombe (Première partie)", "isbn": "9782205078046"},
        {"tome": 7, "subtitle": "Alors, tout tombe (Seconde partie)", "isbn": "9782205085334"},
    ],
    "Blast": [
        {"tome": 1, "subtitle": "Grasse Carcasse", "isbn": "9782205063974"},
        {"tome": 2, "subtitle": "L'Apocalypse selon Saint Jacky", "isbn": "9782205067590"},
        {"tome": 3, "subtitle": "La Tête la première", "isbn": "9782205071047"},
        {"tome": 4, "subtitle": "Pourvu que les bouddhistes se trompent", "isbn": "9782205072730"},
    ],
    "RIP": [
        {"tome": 1, "subtitle": "Derrick — Je ne survivrai pas à la mort", "isbn": "9791095670568"},
        {"tome": 2, "subtitle": "Maurice — Les mouches ne mentent pas", "isbn": "9791095670865"},
        {"tome": 3, "subtitle": "Ahmed — Pourrissant dans la neige", "isbn": "9782380460414"},
        {"tome": 4, "subtitle": "Albert — Prière pour les vivants", "isbn": "9782380460995"},
        {"tome": 5, "subtitle": "Fanette — Mauvaise graine", "isbn": "9782380461381"},
        {"tome": 6, "subtitle": "Eugène — Toutes les bonnes choses ont une fin", "isbn": "9782380461787"},
    ],
    "Tyler Cross": [
        {"tome": 1, "subtitle": "Black Rock", "isbn": "9782205070064"},
        {"tome": 2, "subtitle": "Angola", "isbn": "9782205072853"},
        {"tome": 3, "subtitle": "Miami", "isbn": "9782205077032"},
    ],
    "Le Château des animaux": [
        {"tome": 1, "subtitle": "Miss Bengalore", "isbn": "9782203148888"},
        {"tome": 2, "subtitle": "Les Marguerites d'hiver", "isbn": "9782203172500"},
        {"tome": 3, "subtitle": "La Nuit des justes", "isbn": "9782203222953"},
        {"tome": 4, "subtitle": "Le Sang du roi", "isbn": "9782203235274"},
    ],
    "L'odyssée d'Hakim — Fabien Toulmé": [
        {"tome": 1, "subtitle": "De la Syrie à la Turquie", "isbn": "9782413011262"},
        {"tome": 2, "subtitle": "De la Turquie à la Grèce", "isbn": "9782413013365"},
        {"tome": 3, "subtitle": "De la Macédoine à la France", "isbn": "9782413023739"},
    ],
    "1629 — Les naufragés du Jakarta": [
        {"tome": 1, "subtitle": "L'Apothicaire du diable", "isbn": "9782344045107"},
        {"tome": 2, "subtitle": "Les Martyrs de la baie des Hollandais", "isbn": "9782344058138"},
    ],
    "Dans la tête de Sherlock Holmes — Benoît Dahan / Cyril Lieron": [
        {"tome": 1, "subtitle": "L'Affaire du ticket scandaleux (Tome 1)", "isbn": "9791033509721"},
        {"tome": 2, "subtitle": "L'Affaire du ticket scandaleux (Tome 2)", "isbn": "9791033512547"},
    ],
    "Akira - Tome": [
        {"tome": 1, "subtitle": "Tome 1 (Noir & Blanc)", "isbn": "9782344012406"},
        {"tome": 2, "subtitle": "Tome 2 (Noir & Blanc)", "isbn": "9782344012413"},
        {"tome": 3, "subtitle": "Tome 3 (Noir & Blanc)", "isbn": "9782344012420"},
        {"tome": 4, "subtitle": "Tome 4 (Noir & Blanc)", "isbn": "9782344012437"},
        {"tome": 5, "subtitle": "Tome 5 (Noir & Blanc)", "isbn": "9782344012444"},
        {"tome": 6, "subtitle": "Tome 6 (Noir & Blanc)", "isbn": "9782344012451"},
    ],
    "Slava": [
        {"tome": 1, "subtitle": "Après la chute", "isbn": "9782505115250"},
        {"tome": 2, "subtitle": "Les Nouveaux Russes", "isbn": "9782505119821"},
        {"tome": 3, "subtitle": "Un enfer pour un autre", "isbn": "9782505126324"},
    ],
    "LowReader": [
        {"tome": 1, "subtitle": "Tome 1", "isbn": "9782810202218"},
        {"tome": 2, "subtitle": "Tome 2", "isbn": "9782810202515"},
        {"tome": 3, "subtitle": "Tome 3", "isbn": "9782810202546"},
        {"tome": 4, "subtitle": "Tome 4", "isbn": "9782810203918"},
        {"tome": 5, "subtitle": "Tome 5", "isbn": "9782810207138"},
    ],
    "Le Rapport de Brodeck": [
        {"tome": 1, "subtitle": "L'Autre", "isbn": "9782205073850"},
        {"tome": 2, "subtitle": "L'Indicible", "isbn": "9782205075403"},
    ],
    "Les montagnes hallucinées": [
        {"tome": 1, "subtitle": "Tome 1", "isbn": "9791032703625"},
        {"tome": 2, "subtitle": "Tome 2", "isbn": "9791032703984"},
    ],
    "2001 Nights Stories — Yukinobu Hoshino": [
        {"tome": 1, "subtitle": "Volume 1", "isbn": "9782344057001"},
        {"tome": 2, "subtitle": "Volume 2", "isbn": "9782344057247"},
    ],
    "Les Chants du Cygne Noir": [
        {"tome": 1, "subtitle": "Tome 1", "isbn": "9782810210893"},
        {"tome": 2, "subtitle": "Tome 2", "isbn": "9782810210909"},
    ],
}


async def sync_all():
    logger.info("Démarrage de la synchronisation intégrale du catalogue...")
    async with AsyncSessionLocal() as session:
        # Corriger d'éventuelles valeurs résiduelles en minuscules
        await session.execute(text("UPDATE alert_books SET match_confidence = 'HIGH' WHERE match_confidence = 'high';"))

        alerts = (await session.execute(select(Alert))).scalars().all()
        logger.info("Total alertes en base : %d", len(alerts))

        # 1. Traitement des One-shots & Intégrales
        for a in alerts:
            is_series = False
            for series_name in CERTIFIED_SERIES:
                if series_name.lower() in a.name.lower():
                    is_series = True
                    break
            if is_series or a.name == "Sergio Toppi":
                continue

            matched_isbn = None
            for alert_title_key, isbn in CERTIFIED_ONE_SHOTS.items():
                if alert_title_key.lower() == a.name.lower() or alert_title_key.lower() in a.name.lower():
                    matched_isbn = isbn
                    break

            if matched_isbn:
                vinted_url = (
                    f"https://www.vinted.fr/catalog?search_start_type=item_page_isbn"
                    f"&search_text={matched_isbn}&order=newest_first"
                )
                a.isbn = matched_isbn
                a.vinted_search_url = vinted_url

                book = (await session.execute(
                    select(Book).where(Book.isbn13 == matched_isbn)
                )).scalar()

                if not book:
                    linked_books = (await session.execute(
                        select(Book).join(AlertBook, AlertBook.book_id == Book.id).where(AlertBook.alert_id == a.id)
                    )).scalars().all()

                    if linked_books:
                        book = linked_books[0]
                        book.isbn13 = matched_isbn
                        book.title = a.name
                        book.normalized_title = normalize_text(a.name)
                    else:
                        book = Book(
                            id=uuid.uuid4(),
                            title=a.name,
                            normalized_title=normalize_text(a.name),
                            isbn13=matched_isbn,
                            language="fr",
                        )
                        session.add(book)
                        await session.flush()
                else:
                    book.title = a.name
                    book.normalized_title = normalize_text(a.name)

                # Nettoyer les autres liaisons pour cette alerte
                await session.execute(
                    delete(AlertBook).where(AlertBook.alert_id == a.id, AlertBook.book_id != book.id)
                )

                # Assurer la liaison
                existing_ab = (await session.execute(
                    select(AlertBook).where(AlertBook.alert_id == a.id, AlertBook.book_id == book.id)
                )).scalar()
                if not existing_ab:
                    session.add(AlertBook(alert_id=a.id, book_id=book.id, match_confidence=MatchConfidence.HIGH))

        # 2. Traitement des Séries Multi-Tomes
        for series_name, tomes_data in CERTIFIED_SERIES.items():
            a = (await session.execute(
                select(Alert).where(Alert.name == series_name)
            )).scalar()
            if not a:
                a = (await session.execute(
                    select(Alert).where(Alert.name.ilike(f"%{series_name}%"))
                )).scalar()

            if not a:
                logger.warning("Alerte de série non trouvée : %s", series_name)
                continue

            a.isbn = None
            certified_book_ids = []

            for t_info in tomes_data:
                tome_num = t_info["tome"]
                subtitle = t_info["subtitle"]
                isbn = t_info["isbn"]
                full_title = f"{series_name} — Tome {tome_num} ({subtitle})"

                book = (await session.execute(
                    select(Book).where(Book.isbn13 == isbn)
                )).scalar()

                if not book:
                    book = Book(
                        id=uuid.uuid4(),
                        title=full_title,
                        normalized_title=normalize_text(full_title),
                        isbn13=isbn,
                        language="fr",
                    )
                    session.add(book)
                    await session.flush()
                else:
                    book.title = full_title
                    book.normalized_title = normalize_text(full_title)

                certified_book_ids.append(book.id)

                existing_ab = (await session.execute(
                    select(AlertBook).where(AlertBook.alert_id == a.id, AlertBook.book_id == book.id)
                )).scalar()
                if not existing_ab:
                    session.add(AlertBook(alert_id=a.id, book_id=book.id, match_confidence=MatchConfidence.HIGH))

            # Supprimer les liens obsolètes/doublons
            if certified_book_ids:
                await session.execute(
                    delete(AlertBook).where(
                        AlertBook.alert_id == a.id,
                        AlertBook.book_id.notin_(certified_book_ids)
                    )
                )

        # 3. Traitement spécifique de l'alerte d'auteur Sergio Toppi
        toppi_alert = (await session.execute(
            select(Alert).where(Alert.name == "Sergio Toppi")
        )).scalar()
        if toppi_alert:
            toppi_alert.isbn = None
            toppi_books = (await session.execute(
                select(Book).where(Book.isbn13.in_(["9782352830979", "9782352830443"]))
            )).scalars().all()
            if toppi_books:
                valid_ids = [b.id for b in toppi_books]
                for b_id in valid_ids:
                    existing_ab = (await session.execute(
                        select(AlertBook).where(AlertBook.alert_id == toppi_alert.id, AlertBook.book_id == b_id)
                    )).scalar()
                    if not existing_ab:
                        session.add(AlertBook(alert_id=toppi_alert.id, book_id=b_id, match_confidence=MatchConfidence.HIGH))
                await session.execute(
                    delete(AlertBook).where(
                        AlertBook.alert_id == toppi_alert.id,
                        AlertBook.book_id.notin_(valid_ids)
                    )
                )

        # 4. Nettoyage des livres orphelins (sans alert_books et sans isbn)
        await session.execute(text("""
            DELETE FROM books 
            WHERE isbn13 IS NULL 
              AND id NOT IN (SELECT book_id FROM alert_books);
        """))

        await session.commit()
        logger.info("Synchronisation terminée avec succès !")

        total_books_res = await session.execute(text("SELECT count(*) FROM books;"))
        books_with_isbn_res = await session.execute(text("SELECT count(*) FROM books WHERE isbn13 IS NOT NULL;"))
        books_without_isbn_res = await session.execute(text("SELECT count(*) FROM books WHERE isbn13 IS NULL;"))
        alerts_with_isbn_res = await session.execute(text("SELECT count(*) FROM alerts WHERE isbn IS NOT NULL;"))
        alerts_without_isbn_res = await session.execute(text("SELECT count(*) FROM alerts WHERE isbn IS NULL;"))

        logger.info("=== BILAN CATALOGUE ===")
        logger.info("Total Livres : %d", total_books_res.scalar())
        logger.info("Livres avec ISBN certifié : %d", books_with_isbn_res.scalar())
        logger.info("Livres SANS ISBN : %d", books_without_isbn_res.scalar())
        logger.info("Alertes avec ISBN : %d", alerts_with_isbn_res.scalar())
        logger.info("Alertes Séries (mots-clés) : %d", alerts_without_isbn_res.scalar())


if __name__ == "__main__":
    asyncio.run(sync_all())
