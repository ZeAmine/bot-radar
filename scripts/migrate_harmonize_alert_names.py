"""Script de migration pour harmoniser les noms d'alertes et de livres.

Supprime les noms d'auteurs des titres d'alertes et de livres pour ne garder
que les titres épurés (homogénéité du catalogue), tout en conservant ou enrichissant
les auteurs dans `books.author` et `alerts.include_keywords`.
"""

import asyncio
import logging
from typing import Optional

from sqlalchemy import select, update
from app.db.session import AsyncSessionLocal
from app.db.models.alert import Alert
from app.db.models.book import Book

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("harmonize_names")

ALERT_RENAMES: list[tuple[str, str, list[str]]] = [
    ("1984 — Xavier Coste", "1984", ["coste"]),
    ("2001 Nights Stories — Yukinobu Hoshino", "2001 Nights Stories", ["hoshino"]),
    ("20th Century Boys — Naoki Urasawa", "20th Century Boys", ["urasawa"]),
    ("Akira - Tome", "Akira", []),
    ("Carbone & Silicium — Mathieu Bablet", "Carbone & Silicium", ["bablet"]),
    (
        "Cauchon… ou l'homme qui tua Jeanne d'Arc — Xavier Dorison / Louis-David Delahaye / Joël Parnotte",
        "Cauchon… ou l'homme qui tua Jeanne d'Arc",
        ["dorison"],
    ),
    ("Demon — Jason Shiga", "Demon", ["shiga"]),
    ("Dracula — Bess", "Dracula", ["bess"]),
    ("Frankenstein — Georges Bess / David Sala", "Frankenstein", ["bess"]),
    ("Jesse James — Dobbs / Christophe Regnault", "Jesse James", ["dobbs", "regnault"]),
    ("Journal de 1985 — Xavier Coste", "Journal de 1985", ["coste"]),
    ("L'Enfer de Dante — Paul et Gaëtan Brizzi", "L'Enfer de Dante", ["brizzi"]),
    ("L'Orfèvre — Lozes", "L'Orfèvre", ["lozes"]),
    ("L'odyssée d'Hakim — Fabien Toulmé", "L'odyssée d'Hakim", ["toulme"]),
    ("La Bête — Chabouté", "La Bête", ["chaboute"]),
    ("La Cage aux cons — Robin Recht / Matthieu Angotti", "La Cage aux cons", ["recht"]),
    ("La Favorite — Matthias Lehmann", "La Favorite", ["lehmann"]),
    ("La Nuit — Philippe Druillet", "La Nuit", ["druillet"]),
    ("La Route — Manu Larcenet", "La Route", ["larcenet"]),
    ("Le Collectionneur — Sergio Toppi", "Le Collectionneur", ["toppi"]),
    ("Le Loup des Mers — Riff Reb's", "Le Loup des Mers", ["riff"]),
    ("Le Roi Méduse — Brecht Evens", "Le Roi Méduse", ["evens"]),
    (
        "Les Voyages d'Ulysse — Emmanuel Lepage / René Follet / Sophie Michel",
        "Les Voyages d'Ulysse",
        ["lepage"],
    ),
    ("Minor Arcana — Jeff Lemire", "Minor Arcana", ["lemire"]),
    ("Notre-Dame de Paris — Bess", "Notre-Dame de Paris", ["bess"]),
    ("P.T.S.D. — Guillaume Singelin", "P.T.S.D.", ["singelin"]),
    ("Persepolis — Marjane Satrapi", "Persepolis", ["satrapi"]),
    ("Pulp — Ed Brubaker", "Pulp", ["brubaker"]),
    ("Raptor — Dave McKean", "Raptor", ["mckean"]),
    ("Shangri-La — Mathieu Bablet", "Shangri-La", ["bablet"]),
    ("Sharaz-De — Sergio Toppi", "Sharaz-De", ["toppi"]),
    ("These Savage Shores — Ram V / Sumit Kumar", "These Savage Shores", ["ram v"]),
    ("Tout seul — Chabouté", "Tout seul", ["chaboute"]),
    (
        "Ulysse & Cyrano — Antoine Cristau / Xavier Dorison / Stéphane Servain",
        "Ulysse & Cyrano",
        ["dorison"],
    ),
]

BOOK_EXACT_RENAMES: list[tuple[str, str, str]] = [
    ("1984 — Xavier Coste", "1984", "Xavier Coste"),
    ("20th Century Boys — Naoki Urasawa", "20th Century Boys", "Naoki Urasawa"),
    ("Carbone & Silicium — Mathieu Bablet", "Carbone & Silicium", "Mathieu Bablet"),
    (
        "Cauchon… ou l'homme qui tua Jeanne d'Arc — Xavier Dorison / Louis-David Delahaye / Joël Parnotte",
        "Cauchon… ou l'homme qui tua Jeanne d'Arc",
        "Xavier Dorison / Louis-David Delahaye / Joël Parnotte",
    ),
    ("Demon — Jason Shiga", "Demon", "Jason Shiga"),
    ("Dracula — Bess", "Dracula", "Georges Bess"),
    ("Frankenstein — Georges Bess / David Sala", "Frankenstein", "Georges Bess / David Sala"),
    ("Jesse James — Dobbs / Christophe Regnault", "Jesse James", "Dobbs / Christophe Regnault"),
    ("Journal de 1985 — Xavier Coste", "Journal de 1985", "Xavier Coste"),
    ("L'Enfer de Dante — Paul et Gaëtan Brizzi", "L'Enfer de Dante", "Paul et Gaëtan Brizzi"),
    ("L'Orfèvre — Lozes", "L'Orfèvre", "Lozes"),
    ("La Bête — Chabouté", "La Bête", "Christophe Chabouté"),
    ("La Cage aux cons — Robin Recht / Matthieu Angotti", "La Cage aux cons", "Robin Recht / Matthieu Angotti"),
    ("La Favorite — Matthias Lehmann", "La Favorite", "Matthias Lehmann"),
    ("La Nuit — Philippe Druillet", "La Nuit", "Philippe Druillet"),
    ("La Route — Manu Larcenet", "La Route", "Manu Larcenet"),
    ("Le Collectionneur — Sergio Toppi", "Le Collectionneur", "Sergio Toppi"),
    ("Le Loup des Mers — Riff Reb's", "Le Loup des Mers", "Riff Reb's"),
    ("Le Monde sans fin — Christophe Blain / Jean-Marc Jancovici", "Le Monde sans fin", "Christophe Blain / Jean-Marc Jancovici"),
    ("Le Roi Méduse — Brecht Evens", "Le Roi Méduse", "Brecht Evens"),
    (
        "Les Voyages d'Ulysse — Emmanuel Lepage / René Follet / Sophie Michel",
        "Les Voyages d'Ulysse",
        "Emmanuel Lepage / René Follet / Sophie Michel",
    ),
    ("Minor Arcana — Jeff Lemire", "Minor Arcana", "Jeff Lemire"),
    ("Notre-Dame de Paris — Bess", "Notre-Dame de Paris", "Georges Bess"),
    ("P.T.S.D. — Guillaume Singelin", "P.T.S.D.", "Guillaume Singelin"),
    ("Persepolis — Marjane Satrapi", "Persepolis", "Marjane Satrapi"),
    ("Pulp — Ed Brubaker", "Pulp", "Ed Brubaker"),
    ("Raptor — Dave McKean", "Raptor", "Dave McKean"),
    ("Shangri-La — Mathieu Bablet", "Shangri-La", "Mathieu Bablet"),
    ("Sharaz-De — Sergio Toppi", "Sharaz-De", "Sergio Toppi"),
    ("These Savage Shores — Ram V / Sumit Kumar", "These Savage Shores", "Ram V / Sumit Kumar"),
    ("Tout seul — Chabouté", "Tout seul", "Christophe Chabouté"),
    (
        "Ulysse & Cyrano — Antoine Cristau / Xavier Dorison / Stéphane Servain",
        "Ulysse & Cyrano",
        "Antoine Cristau / Xavier Dorison / Stéphane Servain",
    ),
]


async def run_migration():
    logger.info("Démarrage de l'harmonisation des alertes et des livres...")
    async with AsyncSessionLocal() as session:
        # 1. Mise à jour des alertes
        alerts_updated = 0
        for old_name, new_name, extra_kws in ALERT_RENAMES:
            res = await session.execute(select(Alert).where(Alert.name == old_name))
            alert = res.scalars().first()
            if alert:
                alert.name = new_name
                current_kws = list(alert.include_keywords or [])
                for kw in extra_kws:
                    if kw.lower() not in [k.lower() for k in current_kws]:
                        current_kws.append(kw)
                alert.include_keywords = current_kws
                alerts_updated += 1
                logger.info(f"Alerte MAJ: '{old_name}' -> '{new_name}' (kws={current_kws})")

        # 2. Mise à jour des livres exacts
        books_updated = 0
        for old_title, new_title, author in BOOK_EXACT_RENAMES:
            res = await session.execute(select(Book).where(Book.title == old_title))
            book = res.scalars().first()
            if book:
                book.title = new_title
                book.author = author
                books_updated += 1
                logger.info(f"Livre MAJ: '{old_title}' -> '{new_title}' (auteur='{author}')")

        # 3. Mise à jour des séries avec tomes / patterns
        # 3.1 Akira - Tome — Tome X -> Akira — Tome X
        akira_res = await session.execute(select(Book).where(Book.title.like("Akira - Tome — %")))
        for b in akira_res.scalars().all():
            new_title = b.title.replace("Akira - Tome — ", "Akira — ")
            b.title = new_title
            b.author = "Katsuhiro Ōtomo"
            books_updated += 1
            logger.info(f"Livre Akira MAJ: '{new_title}' (auteur='Katsuhiro Ōtomo')")

        # 3.2 2001 Nights Stories — Yukinobu Hoshino — Tome X -> 2001 Nights Stories — Tome X
        hoshino_res = await session.execute(
            select(Book).where(Book.title.like("2001 Nights Stories — Yukinobu Hoshino — %"))
        )
        for b in hoshino_res.scalars().all():
            new_title = b.title.replace(
                "2001 Nights Stories — Yukinobu Hoshino — ", "2001 Nights Stories — "
            )
            b.title = new_title
            b.author = "Yukinobu Hoshino"
            books_updated += 1
            logger.info(f"Livre 2001 Nights MAJ: '{new_title}' (auteur='Yukinobu Hoshino')")

        # 3.3 L'odyssée d'Hakim — Fabien Toulmé — Tome X -> L'odyssée d'Hakim — Tome X
        hakim_res = await session.execute(
            select(Book).where(Book.title.like("L'odyssée d'Hakim — Fabien Toulmé — %"))
        )
        for b in hakim_res.scalars().all():
            new_title = b.title.replace(
                "L'odyssée d'Hakim — Fabien Toulmé — ", "L'odyssée d'Hakim — "
            )
            b.title = new_title
            b.author = "Fabien Toulmé"
            books_updated += 1
            logger.info(f"Livre L'odyssée d'Hakim MAJ: '{new_title}' (auteur='Fabien Toulmé')")

        await session.commit()
        logger.info(
            f"✅ Harmonisation terminée avec succès ! Alertes modifiées: {alerts_updated}, Livres modifiés: {books_updated}."
        )


if __name__ == "__main__":
    asyncio.run(run_migration())
