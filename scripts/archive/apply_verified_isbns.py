"""Applique les 69 ISBN certifiés et vérifiés sur les alertes et le catalogue de livres en base de données.

- 69 alertes avec ISBN certifié (dont les 8 intégrales spécifiées par l'utilisateur).
- 33 séries multi-tomes maintenues explicitement avec isbn = NULL (incluant Les Gorilles du général).
- Synchronisation complète entre les tables alerts, books et alert_books.
- Nettoyage des caches et ISBN orphelins ou erronés.
"""

import asyncio
from sqlalchemy import text
from app.db.session import AsyncSessionLocal
from app.services.matcher_service import normalize_text

# Dictionnaire {alert_id: isbn13} pour les 69 alertes avec ISBN certifié
VERIFIED_ISBNS: dict[str, str] = {
    # 2 alertes déjà validées
    "4b4b0eec-70db-4dd0-a31e-465dff7a88ca": "9791034768974",  # Soli Deo Gloria
    "fab5b7de-31d5-470a-9066-f98f84c089b7": "9782381961903",  # Watership Down
    # 59 One-shots certifiés
    "4d8e50de-8424-4170-a5e0-8a479f5bc81d": "9782377316588",  # 1984 — Xavier Coste
    "08868ea1-90bb-4775-b14c-87553ce460be": "9782759500079",  # Amer béton — Intégrale
    "5b5d70c3-ee82-4afe-9876-2cf328be6cc4": "9782380354119",  # Bellicus
    "69ead84f-8292-4a61-95b3-e16c063bec62": "9791033511960",  # Carbone & Silicium
    "394bf3a9-d4e0-403c-9763-9ea98e810246": "9782377317929",  # Carcajou
    "cc7fbd61-aee8-4bad-8a43-b131c530f343": "9782505120018",  # Cauchon… ou l'homme qui tua Jeanne d'Arc
    "d432e352-d915-457f-add0-7a3424c220a4": "9791026822363",  # Do a Powerbomb
    "7ea7b297-7fd1-4110-872d-7f932c758c7e": "9782344038604",  # Dracula — Bess
    "7fdb6ab3-2287-4cab-bcd8-ab2cc01b7fe3": "9782344040522",  # Frankenstein — Bess
    "b4a74804-07ce-4ce9-9a58-c365fa164d86": "9782808507585",  # Gunnar le vampire
    "8c2ae8a7-9a07-4908-a8ce-063cc33aeef5": "9782810202294",  # Hoka Hey!
    "acd5ef33-4769-4f48-91d2-eec49248d9fa": "9782302043114",  # Hommes à la mer
    "93790b85-022f-42fd-9f9d-f30b396df5ce": "9782344069554",  # Hélène de Wyndhorn
    "e217bcd4-933f-4a69-8470-ca0823c9d1db": "9782344038482",  # Jesse James
    "1243acaa-5cc8-4020-b6d5-721f7f09748a": "9791040806370",  # Journal de 1985
    "409c3ad1-430e-4685-b7dd-da03be0fcfe0": "9782356741349",  # L'Enfer de Dante
    "e52ef183-478d-4dde-84ea-1747ef6c0355": "9782491374969",  # L'Orfèvre — Lozes
    "427b32eb-c6b4-47c4-9299-2176049bc3b3": "9782505078647",  # La Bibliomule de Cordoue
    "bbc21e9f-effe-4212-8a61-1f2db6acf5f5": "9782413018575",  # La Cage aux cons
    "bdc0f0ab-bdea-4a9b-8b31-1ca26a9c07b3": "9782330047788",  # La Favorite — Lehmann
    "4eaec19b-e11a-401c-8c50-18a1324eb75c": "9782205208153",  # La Route — Larcenet
    "b6a682e7-2cd2-42bd-8c3a-687360b05698": "9782413076780",  # La Terre verte
    "fa64b67c-ec31-4a33-9f83-292cab2446d8": "9782808502665",  # La Tête de mort venue de Suède
    "947c7b62-9433-4684-871a-ae28a92856b9": "9782810207121",  # La langue des vipères
    "6b7d24d7-1de2-42ad-bc18-296596eeec4d": "9782302024359",  # Le Loup des Mers — Riff Reb's
    "01e051f8-1c63-4240-8aee-c0780ae2bc0f": "9782505063421",  # Le Maître d'armes
    "ef0b088e-e9e5-4183-9976-2d5e4de1b902": "9782205088168",  # Le Monde sans fin
    "53e55389-6bd9-4e11-b955-85a8852386b8": "9782203215764",  # Le Poids des héros
    "b7b2dc6f-a0ff-4bc1-a202-0012a5012497": "9782756035734",  # Les Indes fourbes
    "7d66a852-e8a0-4c2e-9f3c-b137f2530329": "9782356740434",  # Les Voyages d'Ulysse
    "5e423a11-dc28-4830-80bf-2bd9a1b7b828": "9782344047033",  # Les travailleurs de la mer
    "7c2627d2-9877-46e9-bfde-4de09778a927": "9782344054147",  # Notre-Dame de Paris — Bess
    "144995cd-708d-431a-86b6-ec79dda88c87": "9791033509530",  # P.T.S.D. — Singelin
    "a0caa105-bc99-419d-a1ac-c23f6994f394": "9782413044932",  # Perpendiculaire au soleil
    "c1050f1f-2442-48f4-9231-71aa3386487d": "9782749310015",  # Plus loin qu'ailleurs
    "739f6bdf-9cd4-484e-b059-71f41801ec91": "9782413039518",  # Pulp — Ed Brubaker
    "b4e5e336-a488-4e2a-84e9-18d86704f1ea": "9782754832939",  # Raptor — Dave McKean
    "60ef3be8-517b-466f-b928-70c8cf5fe456": "9782344034200",  # Sangoma
    "6e66c2da-f800-4d26-a90a-4450c7d71693": "9782359109696",  # Shangri-La — Bablet
    "4443ffef-4eaa-4577-80f2-f6cee7cc9fa0": "9782352830979",  # Sharaz-De — Sergio Toppi
    "feb48dee-b49f-4c9c-b1ec-91458ef3cbcd": "9782810208005",  # Silent Jenny
    "577edcfe-0d07-4abb-b0e6-db2585a0050d": "9791039147125",  # Spider-Man — Kraven
    "1b1d2869-c04c-4ff2-a198-31baaf1302c5": "9782205089752",  # T'Zée — Une tragédie africaine
    "ed72b118-bfdd-45fa-ad57-826d9e066ec9": "9782344069721",  # The Junction (Alerte 1)
    "1e7682e8-5a28-46ac-92d0-fab94ea7627e": "9782344069721",  # The Junction (Alerte 2)
    "c4030ea0-cabb-43fd-8e7f-6f762a329f46": "9782378871604",  # These Savage Shores
    "79a545c5-57e9-469c-b4fc-9e19f75663fe": "9782356741929",  # Thrillerville
    "0870ea3a-cef0-4b1f-bf61-a2296c54216d": "9782749304298",  # Tout seul — Chabouté
    "eb7c20f9-16e7-404b-b02c-e514086bc9b2": "9782387250001",  # Train de nuit dans la Voie lactée
    "e8c6b34e-977f-44fc-8261-e36293920191": "9782203171473",  # Ulysse & Cyrano
    "6085a2d3-2406-4473-a3a4-975b86eb7d55": "9782756062105",  # Un océan d'amour
    "263e0ae6-1571-4243-941e-d196f96a7456": "9782754816953",  # Winter Road
    "60153364-ef67-4347-8a94-2c8fb9fa2b5f": "9791032408162",  # Âme augmentée
    "80912b7a-ede8-4ba3-bd5a-a3c71d0e87c8": "9782365771788",  # Batman — Un long Halloween
    "2a1d577c-5f8e-414d-bfa2-f63e678bbb4b": "9782365770156",  # Batman — Amère victoire
    "5b8a7560-fafb-4775-a2f2-e924828e0662": "9782365770156",  # Batman — Dark Victory
    "d98652a1-e401-4f63-b722-4de85257b2e7": "9791026812715",  # Batman Terre Un
    "84bb283d-5176-45f9-bb17-224747e57c10": "9782366243666",  # Demon — Jason Shiga
    "b9ec4fea-83a1-41ad-a290-be9c008fd110": "9782365770095",  # Watchmen
    # 8 Intégrales certifiées par l'utilisateur
    "1d41ed1a-f035-4bc2-afb0-6993adfea3b9": "9791033512431",  # Mutafukaz' Loba Loca - L'intégrale
    "d567e3e3-25f8-4a7c-a089-6b4d2b29da10": "9791033504849",  # Mutafukaz puta madre integrale
    "26c90974-0ce4-433f-8fa0-b9050a4a8b03": "9782359109870",  # Adrastée intégrale
    "920e0161-fa34-46fc-8c29-c8c0b88570f2": "9782754841764",  # Urban - Intégrale
    "f69304fe-ec91-4b47-8b03-104d00c32631": "9782800142449",  # La guerre éternelle — Intégrale
    "5b46df59-7ef0-4e67-8c11-22a25bc4a6ab": "9782809488203",  # Daredevil - L'homme sans peur
    "9e250337-3153-4ae3-9e9a-c6be1d66704a": "9791033504573",  # The Grocery l’intégrale
    "a08fbccd-503f-49c1-8041-0314e887bfac": "9791033509301",  # Monkey Bizness – L’intégrale
}


async def main() -> None:
    print(f"Début de l'application des {len(VERIFIED_ISBNS)} ISBNs certifiés...")

    async with AsyncSessionLocal() as session:
        # 1. Vérifier le total d'alertes en base
        result = await session.execute(text("SELECT id, name FROM alerts;"))
        all_alerts = result.fetchall()
        print(f"Total alertes existantes en base : {len(all_alerts)}")

        # 2. Réinitialiser tous les ISBN à NULL
        res_null = await session.execute(text("UPDATE alerts SET isbn = NULL;"))
        print(f"Alertes réinitialisées à isbn = NULL : {res_null.rowcount}")

        # Nettoyer les ISBNs dans books pour les alertes à isbn = NULL (ex: puzzle book des Gorilles, tomes de séries)
        await session.execute(text("""
            UPDATE books SET isbn13 = NULL
            WHERE id IN (
                SELECT ab.book_id
                FROM alerts a
                JOIN alert_books ab ON a.id = ab.alert_id
                WHERE a.isbn IS NULL
            );
        """))
        # Invalider les caches obsolètes pour ces séries/livres sans ISBN
        await session.execute(text("""
            DELETE FROM book_market_cache
            WHERE book_id IN (
                SELECT ab.book_id
                FROM alerts a
                JOIN alert_books ab ON a.id = ab.alert_id
                WHERE a.isbn IS NULL
            );
        """))

        # 3. Mettre à jour chaque alerte certifiée et synchroniser le catalogue books
        updated_count = 0
        for alert_id, isbn in VERIFIED_ISBNS.items():
            # Mise à jour de l'alerte
            vinted_url = (
                f"https://www.vinted.fr/catalog?search_start_type=item_page_isbn"
                f"&search_text={isbn}&order=newest_first"
            )
            res = await session.execute(
                text("""
                    UPDATE alerts 
                    SET isbn = :isbn, vinted_search_url = :url
                    WHERE id = :id
                """),
                {"isbn": isbn, "url": vinted_url, "id": alert_id},
            )
            updated_count += res.rowcount

            # Récupération du nom de l'alerte
            alert_name_res = await session.execute(
                text("SELECT name FROM alerts WHERE id = :id"),
                {"id": alert_id},
            )
            alert_name = alert_name_res.scalar() or ""

            # Synchronisation du livre correspondant
            book_res = await session.execute(
                text("SELECT id FROM books WHERE isbn13 = :isbn"),
                {"isbn": isbn},
            )
            existing_book_id = book_res.scalar()

            if existing_book_id:
                # Assurer le lien dans alert_books
                await session.execute(
                    text("""
                        INSERT INTO alert_books (alert_id, book_id, match_confidence, created_at)
                        VALUES (:alert_id, :book_id, 'high', NOW())
                        ON CONFLICT (alert_id, book_id) DO NOTHING;
                    """),
                    {"alert_id": alert_id, "book_id": existing_book_id},
                )
            else:
                # Vérifier si un livre est déjà lié à cette alerte
                linked_book_res = await session.execute(
                    text("SELECT book_id FROM alert_books WHERE alert_id = :alert_id"),
                    {"alert_id": alert_id},
                )
                linked_book_id = linked_book_res.scalar()

                if linked_book_id:
                    # Mettre à jour le livre existant
                    await session.execute(
                        text("""
                            UPDATE books 
                            SET isbn13 = :isbn, title = :title, normalized_title = :norm_title
                            WHERE id = :book_id
                        """),
                        {
                            "isbn": isbn,
                            "title": alert_name,
                            "norm_title": normalize_text(alert_name),
                            "book_id": linked_book_id,
                        },
                    )
                else:
                    # Créer un nouveau livre
                    import uuid
                    new_book_id = uuid.uuid4()
                    await session.execute(
                        text("""
                            INSERT INTO books (id, title, normalized_title, isbn13, language, created_at, updated_at)
                            VALUES (:id, :title, :norm_title, :isbn, 'fr', NOW(), NOW());
                        """),
                        {
                            "id": new_book_id,
                            "title": alert_name,
                            "norm_title": normalize_text(alert_name),
                            "isbn": isbn,
                        },
                    )
                    await session.execute(
                        text("""
                            INSERT INTO alert_books (alert_id, book_id, match_confidence, created_at)
                            VALUES (:alert_id, :book_id, 'high', NOW());
                        """),
                        {"alert_id": alert_id, "book_id": new_book_id},
                    )

        # Nettoyage des liaisons incohérentes dans alert_books (où l'alerte a un ISBN mais le livre en a un différent)
        await session.execute(text("""
            DELETE FROM alert_books ab
            USING alerts a, books b
            WHERE ab.alert_id = a.id
              AND ab.book_id = b.id
              AND a.isbn IS NOT NULL
              AND b.isbn13 IS NOT NULL
              AND a.isbn != b.isbn13;
        """))

        await session.commit()

        # 4. Vérification post-mise à jour
        res_with_isbn = await session.execute(
            text("SELECT count(*) FROM alerts WHERE isbn IS NOT NULL;")
        )
        count_with_isbn = res_with_isbn.scalar()

        res_without_isbn = await session.execute(
            text("SELECT count(*) FROM alerts WHERE isbn IS NULL;")
        )
        count_without_isbn = res_without_isbn.scalar()

        books_with_isbn = await session.execute(
            text("SELECT count(*) FROM books WHERE isbn13 IS NOT NULL;")
        )
        count_books_with_isbn = books_with_isbn.scalar()

        print("Mise à jour terminée avec succès !")
        print(f"  - Alertes avec ISBN certifié : {count_with_isbn} (attendu : 69)")
        print(f"  - Alertes en matching textuel (NULL) : {count_without_isbn} (attendu : 33)")
        print(f"  - Total alertes : {count_with_isbn + count_without_isbn} (attendu : 102)")
        print(f"  - Livres avec ISBN certifié : {count_books_with_isbn}")


if __name__ == "__main__":
    asyncio.run(main())
