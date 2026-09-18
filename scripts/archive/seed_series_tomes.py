"""Script de pré-catalogage des tomes pour les séries multi-tomes actives (isbn = NULL).

Associe chaque alerte série à ses tomes officiels dans books et alert_books.
Chaque tome possède son ISBN-13 exact pour permettre au worker d'arrière-plan
de pré-calculer et mettre en cache la cote précise de chaque volume.
"""

import asyncio
import uuid
from sqlalchemy import text
from app.db.session import AsyncSessionLocal
from app.services.matcher_service import normalize_text

# Dictionnaire {alert_name: list_of_tomes}
# Format du tome : (numero_tome, sous_titre, isbn13)
SERIES_TOMES_CATALOG: dict[str, list[tuple[int, str, str]]] = {
    "RIP": [
        (1, "Derrick — Je ne survivrai pas à la mort", "9791095670568"),
        (2, "Maurice — Les mouches ne mentent pas", "9791095670865"),
        (3, "Ahmed — Pourrissant dans la neige", "9782380460414"),
        (4, "Albert — Prière pour les vivants", "9782380460995"),
        (5, "Fanette — Mauvaise graine", "9782380461381"),
        (6, "Eugène — Toutes les bonnes choses ont une fin", "9782380461787"),
    ],
    "Blacksad": [
        (1, "Quelque part entre les ombres", "9782205049657"),
        (2, "Arctic-Nation", "9782205051995"),
        (3, "Âme Rouge", "9782205055641"),
        (4, "L'Enfer, le silence", "9782205063134"),
        (5, "Amarillo", "9782205071801"),
        (6, "Alors, tout tombe (Première partie)", "9782205078046"),
        (7, "Alors, tout tombe (Seconde partie)", "9782205085334"),
    ],
    "Blast": [
        (1, "Grasse Carcasse", "9782205063974"),
        (2, "L'Apocalypse selon Saint Jacky", "9782205067590"),
        (3, "La Tête la première", "9782205071047"),
        (4, "Pourvu que les bouddhistes se trompent", "9782205072730"),
    ],
    "Le Château des animaux": [
        (1, "Miss Bengalore", "9782203148888"),
        (2, "Les Marguerites d'hiver", "9782203172500"),
        (3, "La Nuit des justes", "9782203222953"),
        (4, "Le Sang du roi", "9782203235274"),
    ],
    "L'odyssée d'Hakim — Fabien Toulmé": [
        (1, "De la Syrie à la Turquie", "9782413011262"),
        (2, "De la Turquie à la Grèce", "9782413013365"),
        (3, "De la Macédoine à la France", "9782413023739"),
    ],
    "Tyler Cross": [
        (1, "Black Rock", "9782205070064"),
        (2, "Angola", "9782205072853"),
        (3, "Miami", "9782205077032"),
    ],
    "Dans la tête de Sherlock Holmes — Benoît Dahan / Cyril Lieron": [
        (1, "L'Affaire du ticket scandaleux (Tome 1)", "9791033509721"),
        (2, "L'Affaire du ticket scandaleux (Tome 2)", "9791033512547"),
    ],
    "1629 — Les naufragés du Jakarta": [
        (1, "L'Apothicaire du diable", "9782344045107"),
        (2, "Les Martyrs de la baie des Hollandais", "9782344058138"),
    ],
    "Le Combat Ordinaire": [
        (1, "Le Combat ordinaire", "9782205054255"),
        (2, "Les Quantités négligeables", "9782205055894"),
        (3, "Ce qui est précieux", "9782205057911"),
        (4, "Planter des clous", "9782205061406"),
    ],
}


async def seed_series():
    print("Démarrage du pré-catalogage des tomes pour les séries multi-tomes...")

    async with AsyncSessionLocal() as session:
        total_created = 0
        total_linked = 0

        for alert_name, tomes in SERIES_TOMES_CATALOG.items():
            # Trouver l'alerte correspondante
            alert_res = await session.execute(
                text("SELECT id, name FROM alerts WHERE name = :name"),
                {"name": alert_name},
            )
            alert_row = alert_res.fetchone()
            if not alert_row:
                print(f"⚠️ Alerte non trouvée en base : '{alert_name}'")
                continue

            alert_id = alert_row[0]
            print(f"\nTraitement de la série '{alert_name}' (ID: {alert_id}) : {len(tomes)} tomes")

            for t_num, subtitle, isbn13 in tomes:
                book_title = f"{alert_name} — Tome {t_num} ({subtitle})"
                norm_title = normalize_text(book_title)

                # Vérifier si le livre existe déjà par son ISBN-13
                existing_book_res = await session.execute(
                    text("SELECT id, title FROM books WHERE isbn13 = :isbn"),
                    {"isbn": isbn13},
                )
                existing_book = existing_book_res.fetchone()

                if existing_book:
                    book_id = existing_book[0]
                    # Mettre à jour le titre propre avec le tome
                    await session.execute(
                        text("""
                            UPDATE books 
                            SET title = :title, normalized_title = :norm_title
                            WHERE id = :id
                        """),
                        {"title": book_title, "norm_title": norm_title, "id": book_id},
                    )
                else:
                    book_id = uuid.uuid4()
                    await session.execute(
                        text("""
                            INSERT INTO books (id, title, normalized_title, isbn13, language, created_at, updated_at)
                            VALUES (:id, :title, :norm_title, :isbn, 'fr', NOW(), NOW());
                        """),
                        {
                            "id": book_id,
                            "title": book_title,
                            "norm_title": norm_title,
                            "isbn": isbn13,
                        },
                    )
                    total_created += 1

                # Lier le tome à l'alerte parent dans alert_books
                await session.execute(
                    text("""
                        INSERT INTO alert_books (alert_id, book_id, match_confidence, created_at)
                        VALUES (:alert_id, :book_id, 'high', NOW())
                        ON CONFLICT (alert_id, book_id) DO NOTHING;
                    """),
                    {"alert_id": alert_id, "book_id": book_id},
                )
                total_linked += 1
                print(f"  ✓ Tome {t_num} lié : '{book_title}' (ISBN {isbn13})")

        # Nettoyage des livres orphelins sans ISBN créés précédemment pour ces séries
        for alert_name in SERIES_TOMES_CATALOG.keys():
            await session.execute(text("""
                DELETE FROM alert_books ab
                USING alerts a, books b
                WHERE ab.alert_id = a.id
                  AND ab.book_id = b.id
                  AND a.name = :name
                  AND b.isbn13 IS NULL;
            """), {"name": alert_name})

        await session.commit()
        print("\n" + "=" * 50)
        print(f"Pré-catalogage terminé avec succès !")
        print(f"  - Nouveaux tomes créés : {total_created}")
        print(f"  - Total tomes reliés aux alertes : {total_linked}")


if __name__ == "__main__":
    asyncio.run(seed_series())
