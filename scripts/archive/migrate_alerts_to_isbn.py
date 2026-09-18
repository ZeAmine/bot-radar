"""Script de migration et d'enrichissement ISBN pour toutes les alertes BD du Radar Bot."""

import asyncio
import logging
from sqlalchemy import select

from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.repositories.book_repo import BookRepository
from app.db.session import AsyncSessionLocal
from app.services.matcher_service import normalize_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_alerts_isbn")

ISBN_MAPPING: dict[str, str] = {
    "1629 — Les naufragés du Jakarta": "9782344045107",
    "1984 — Xavier Coste": "9782377316588",
    "2001 Nights Stories — Yukinobu Hoshino": "9782344057247",
    "20th Century Boys — Naoki Urasawa": "9782845386181",
    "Adrastée": "9782359104042",
    "Akira - Tome": "9782723488525",
    "Amer béton — Intégrale": "9782845388055",
    "Batman & Robin — Année Un": "9791026829744",
    "Batman Terre Un": "9791026820130",
    "Batman — Amère victoire": "9791026819448",
    "Batman — Dark Victory": "9791026819448",
    "Batman — Un long Halloween": "9791026819431",
    "Bellicus": "9782380354119",
    "Beneath the Trees Where Nobody Sees": "9791033530435",
    "Blacksad": "9782205078054",
    "Blast": "9782205063974",
    "Carbone & Silicium — Mathieu Bablet": "9782359107630",
    "Carcajou": "9782382640234",
    "Cauchon… ou l'homme qui tua Jeanne d'Arc — Xavier Dorison / Louis-David Delahaye / Joël Parnotte": "9782205089943",
    "Dans la tête de Sherlock Holmes — Benoît Dahan / Cyril Lieron": "9782359104769",
    "Dead Train": "9782413083399",
    "Demon — Jason Shiga": "9782369902348",
    "Do a Powerbomb": "9791026827016",
    "Dracula — Bess": "9782344038604",
    "Frankenstein — Georges Bess / David Sala": "9782344043837",
    "Gunnar le vampire": "9782344062081",
    "Hoka Hey!": "9791033512608",
    "Hommes à la mer": "9782377317769",
    "Hélène de Wyndhorn": "9782205208795",
    "Islander": "9782382640166",
    "Jesse James — Dobbs / Christophe Regnault": "9782344017340",
    "Journal de 1985 — Xavier Coste": "9782377319985",
    "Junk": "9782382640302",
    "L'Enfer de Dante — Paul et Gaëtan Brizzi": "9782368466100",
    "L'Homme sans peur": "9791039121705",
    "L'Orfèvre — Lozes": "9782377318537",
    "L'appel de Cthulhu": "9782359108170",
    "L'odyssée d'Hakim — Fabien Toulmé": "9782413011262",
    "La Bibliomule de Cordoue": "9782205078580",
    "La Cage aux cons — Robin Recht / Matthieu Angotti": "9782413023906",
    "La Favorite — Matthias Lehmann": "9782369902140",
    "La Route — Manu Larcenet": "9782205089752",
    "La Terre verte": "9782205085358",
    "La Tête de mort venue de Suède": "9782377316717",
    "La guerre éternelle — Intégrale": "9782205079631",
    "La langue des vipères": "9782413045052",
    "Le Château des animaux": "9782205078122",
    "Le Château des étoiles — Intégrale": "9782369810148",
    "Le Collectionneur — Sergio Toppi": "9782800174099",
    "Le Combat Ordinaire": "9782205067132",
    "Le Loup des Mers — Riff Reb's": "9782916589947",
    "Le Maître d'armes": "9782205073187",
    "Le Monde sans fin — Christophe Blain / Jean-Marc Jancovici": "9782205088168",
    "Le Poids des héros": "9782205089745",
    "Le Rapport de Brodeck": "9782205073859",
    "Le Roi Méduse — Brecht Evens": "9782330191597",
    "Les Chants du Cygne Noir": "9782205206302",
    "Les Gorilles du général": "9782377317776",
    "Les Indes fourbes": "9782413008149",
    "Les Voyages d'Ulysse — Emmanuel Lepage / René Follet / Sophie Michel": "9782203093607",
    "Les montagnes hallucinées": "9791032704875",
    "Les travailleurs de la mer": "9782723485074",
    "Loba Loca": "9791033511908",
    "LowReader": "9791033513810",
    "Minor Arcana — Jeff Lemire": "9791026829287",
    "Monkey Bizness": "9782359101164",
    "Mutafukaz' Puta Madre": "9791033504368",
    "Notre-Dame de Paris — Bess": "9782344053874",
    "P.T.S.D. — Guillaume Singelin": "9782359108996",
    "Perpendiculaire au soleil": "9782205085389",
    "Persepolis — Marjane Satrapi": "9782844142405",
    "Plus loin qu'ailleurs": "9782413038313",
    "Pulp — Ed Brubaker": "9782413028680",
    "RIP": "9782377430154",
    "Raptor — Dave McKean": "9782413042679",
    "Rubrique-à-Brac — Gotlib": "9782205059618",
    "Sangoma": "9782723498876",
    "Sergio Toppi": "9782800174099",
    "Shangri-La — Mathieu Bablet": "9782359109696",
    "Sharaz-De — Sergio Toppi": "9782800171227",
    "Silent Jenny": "9782359109962",
    "Slava": "9782205089721",
    "Soli Deo Gloria": "9791034768974",
    "Spider-Man — La dernière chasse de Kraven": "9791039121699",
    "T'Zée — Une tragédie africaine": "9782205085396",
    "Terre ou Lune": "9782377317752",
    "The Grocery": "9782359102284",
    "The Junction": "9782359109016",
    "The Nice House by the Sea": "9791026829706",
    "These Savage Shores — Ram V / Sumit Kumar": "9791035501860",
    "Thrillerville": "9782382640098",
    "Tout seul — Chabouté": "9782749909066",
    "Train de nuit dans la Voie lactée": "9782377318728",
    "Tyler Cross": "9782205070058",
    "Ulysse & Cyrano — Antoine Cristau / Xavier Dorison / Stéphane Servain": "9782205088991",
    "Un océan d'amour": "9782413044871",
    "Urban — Intégrale": "9782413040225",
    "Watchmen": "9791026815303",
    "Watership Down": "9782381961903",
    "Winter Road": "9782382640197",
    "Âme augmentée": "9782377317783",
}


async def migrate_alerts():
    logger.info("Démarrage de la migration des alertes vers les ISBNs officiels...")

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Alert))
        alerts = list(result.scalars().all())
        logger.info("%d alertes trouvées en base de données.", len(alerts))

        book_repo = BookRepository(session)
        updated_count = 0

        for alert in alerts:
            isbn = ISBN_MAPPING.get(alert.name)
            if not isbn:
                logger.warning("Aucun mapping ISBN trouvé pour l'alerte: '%s'", alert.name)
                continue

            # Mise à jour de l'alerte
            alert.isbn = isbn
            alert.vinted_search_url = f"https://www.vinted.fr/catalog?search_start_type=item_page_isbn&search_text={isbn}&order=newest_first"

            # Vérifier ou créer le livre associé
            book = await book_repo.get_by_isbn13(isbn)
            if not book:
                book = Book(
                    title=alert.name,
                    normalized_title=normalize_text(alert.name),
                    isbn13=isbn,
                )
                session.add(book)
                await session.flush()

            # Lier l'alerte au livre
            await book_repo.link_alert_to_book(alert_id=alert.id, book_id=book.id)
            updated_count += 1
            logger.info("✅ Alerte mise à jour: '%s' -> ISBN %s", alert.name, isbn)

        await session.commit()
        logger.info(
            "Migration terminée avec succès : %d/%d alertes mises à jour !",
            updated_count,
            len(alerts),
        )


if __name__ == "__main__":
    asyncio.run(migrate_alerts())
