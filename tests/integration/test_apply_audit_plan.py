"""Le plan d'audit est vérifiable sans écrire, atomique, et cumule les retraits.

Ces garanties portent la sûreté de l'outil : un plan périmé ne doit rien modifier,
et plusieurs retraits sur une même alerte ne doivent pas se rétablir l'un l'autre.
"""

import uuid

import pytest
from sqlalchemy import select

from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.book_repo import BookRepository
from scripts.apply_audit_plan import _validate_entry, apply_plan
from tests.database import postgres_replayed_database

pytestmark = pytest.mark.asyncio


async def _seed(maker, *, name="Le Château des animaux", isbn=None, excludes=None):
    async with maker() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_user_id == 89897))
        if user is None:
            user = User(telegram_user_id=89897, is_allowed=True)
            session.add(user)
        category = await session.scalar(select(Category).where(Category.slug == "bd_comics"))
        if category is None:
            category = Category(slug="bd_comics", label="Livres", emoji="📚")
            session.add(category)
        await session.flush()
        alert = Alert(
            id=uuid.uuid4(),
            user_id=user.id,
            category_id=category.id,
            name=name,
            isbn=isbn,
            include_keywords=["chateau"],
            exclude_keywords=list(excludes or ["poche"]),
            variants=[],
            status="paused",
            max_item_price_cents=5000,
        )
        session.add(alert)
        await session.flush()
        return alert.id


async def _link(maker, alert_id, isbn13, title):
    async with maker() as session, session.begin():
        book = Book(title=title, normalized_title=title.lower(), isbn13=isbn13)
        session.add(book)
        await session.flush()
        await BookRepository(session).link_alert_to_book(alert_id=alert_id, book_id=book.id)
        return book.id


async def test_dry_run_reports_everything_and_writes_nothing():
    async with postgres_replayed_database() as database:
        maker = database.maker
        alert_id = await _seed(maker, excludes=["poche"])
        plan = [
            {
                "id": str(alert_id),
                "name": "Le Château des animaux",
                "field": "exclude_keywords",
                "before": ["poche"],
                "after": ["poche", "claudel"],
            }
        ]
        async with maker() as session, session.begin():
            results = await apply_plan(session, plan, apply=False)
        assert [r["status"] for r in results] == ["à corriger"]

        async with maker() as session:
            assert (await session.get(Alert, alert_id)).exclude_keywords == ["poche"]


async def test_apply_then_rerun_is_idempotent():
    async with postgres_replayed_database() as database:
        maker = database.maker
        alert_id = await _seed(maker, excludes=["poche"])
        plan = [
            {
                "id": str(alert_id),
                "name": "Le Château des animaux",
                "field": "exclude_keywords",
                "reason": "le roman de Claudel se fait passer pour la BD",
                "before": ["poche"],
                "after": ["poche", "claudel"],
            }
        ]
        async with maker() as session, session.begin():
            assert await apply_plan(session, plan, apply=True) == [
                {
                    "alert": "Le Château des animaux",
                    "field": "exclude_keywords",
                    "status": "corrigé",
                    "raison": "le roman de Claudel se fait passer pour la BD",
                    "avant": ["poche"],
                    "apres": ["poche", "claudel"],
                },
            ]
        async with maker() as session:
            assert (await session.get(Alert, alert_id)).exclude_keywords == ["poche", "claudel"]

        async with maker() as session, session.begin():
            assert (await apply_plan(session, plan, apply=True))[0]["status"] == "déjà appliqué"


async def test_stale_plan_is_refused_and_writes_nothing():
    async with postgres_replayed_database() as database:
        maker = database.maker
        alert_id = await _seed(maker, excludes=["poche"])
        async with maker() as session, session.begin():
            (await session.get(Alert, alert_id)).exclude_keywords = ["choix manuel"]

        plan = [
            {
                "id": str(alert_id),
                "name": "Le Château des animaux",
                "field": "exclude_keywords",
                "before": ["poche"],
                "after": ["poche", "claudel"],
            }
        ]
        with pytest.raises(ValueError, match="périmé"):
            async with maker() as session, session.begin():
                await apply_plan(session, plan, apply=True)
        async with maker() as session:
            assert (await session.get(Alert, alert_id)).exclude_keywords == ["choix manuel"]


async def test_multiple_unlinks_do_not_restore_each_other():
    """Deux retraits sur une même alerte : le second ne doit pas relier le premier."""
    async with postgres_replayed_database() as database:
        maker = database.maker
        alert_id = await _seed(maker, name="Blast")
        keep = await _link(maker, alert_id, "9782205063974", "Blast — Tome 1 (Grasse Carcasse)")
        await _link(maker, alert_id, "9780613139045", "Blast")
        await _link(maker, alert_id, "9781681375687", "Blast off")

        plan = [
            {
                "id": str(alert_id),
                "name": "Blast",
                "field": "unlink_book",
                "book_isbn13": "9780613139045",
            },
            {
                "id": str(alert_id),
                "name": "Blast",
                "field": "unlink_book",
                "book_isbn13": "9781681375687",
            },
        ]
        async with maker() as session, session.begin():
            await apply_plan(session, plan, apply=True)
        async with maker() as session:
            remaining = await BookRepository(session).get_all_books_for_alert(alert_id)
            assert [b.id for b in remaining] == [keep]


async def test_isbn_removal_detaches_links_and_clears_series_total():
    async with postgres_replayed_database() as database:
        maker = database.maker
        alert_id = await _seed(maker, name="Batman — Dark Victory", isbn="9782365770156")
        await _link(maker, alert_id, "9782365770156", "Batman — Dark Victory")
        async with maker() as session, session.begin():
            (await session.get(Alert, alert_id)).series_total_cents = 1234

        plan = [
            {
                "id": str(alert_id),
                "name": "Batman — Dark Victory",
                "field": "isbn",
                "before": "9782365770156",
                "after": None,
            }
        ]
        async with maker() as session, session.begin():
            await apply_plan(session, plan, apply=True)
        async with maker() as session:
            stored = await session.get(Alert, alert_id)
            assert stored.isbn is None
            assert stored.series_total_cents is None
            assert await BookRepository(session).get_all_books_for_alert(alert_id) == []
            assert len((await session.scalars(select(Book))).all()) == 1


@pytest.mark.parametrize(
    ("entry", "motif"),
    [
        ({"name": "x", "field": "isbn", "before": 1, "after": 2}, "id manquant"),
        ({"id": str(uuid.uuid4()), "field": "isbn", "before": 1, "after": 2}, "name manquant"),
        (
            {"id": str(uuid.uuid4()), "name": "x", "field": "status", "before": 1, "after": 2},
            "champ non supporté",
        ),
        ({"id": str(uuid.uuid4()), "name": "x", "field": "unlink_book"}, "book_isbn13 manquant"),
        ({"id": str(uuid.uuid4()), "name": "x", "field": "isbn"}, "before/after requis"),
    ],
)
async def test_invalid_entries_are_rejected(entry, motif):
    with pytest.raises(ValueError, match=motif):
        _validate_entry(entry)


async def test_isbn_removal_preserves_a_book_shared_with_another_alert():
    """Le contrat F06 : retirer un ISBN ne touche ni le Book partagé ni l'autre alerte."""
    async with postgres_replayed_database() as database:
        maker = database.maker
        dark = await _seed(maker, name="Batman — Dark Victory", isbn="9782365770156")
        amere = await _seed(maker, name="Batman — Amère victoire", isbn="9782365770156")
        async with maker() as session, session.begin():
            book = Book(
                title="Batman — Dark Victory",
                normalized_title="batman — dark victory",
                isbn13="9782365770156",
            )
            session.add(book)
            await session.flush()
            shared = book.id
            link = BookRepository(session).link_alert_to_book
            await link(alert_id=dark, book_id=shared)
            await link(alert_id=amere, book_id=shared)

        plan = [
            {
                "id": str(dark),
                "name": "Batman — Dark Victory",
                "field": "isbn",
                "before": "9782365770156",
                "after": None,
            }
        ]
        async with maker() as session, session.begin():
            await apply_plan(session, plan, apply=True)

        async with maker() as session:
            assert (await session.get(Alert, dark)).isbn is None
            assert await BookRepository(session).get_all_books_for_alert(dark) == []
            assert (await session.get(Alert, amere)).isbn == "9782365770156"
            kept = await BookRepository(session).get_all_books_for_alert(amere)
            assert [b.id for b in kept] == [shared]
            assert await session.get(Book, shared) is not None


async def test_a_late_failed_entry_rolls_back_earlier_unlinks():
    """Un plan refusé en cours de route ne doit laisser aucune dissociation derrière lui."""
    async with postgres_replayed_database() as database:
        maker = database.maker
        alert_id = await _seed(maker, name="Blast", isbn="9782756080642")
        await _link(maker, alert_id, "9782205063974", "Blast — Tome 1")
        await _link(maker, alert_id, "9780613139045", "Blast")
        await _link(maker, alert_id, "9781681375687", "Blast off")

        plan = [
            {
                "id": str(alert_id),
                "name": "Blast",
                "field": "unlink_book",
                "book_isbn13": "9780613139045",
            },
            {
                "id": str(alert_id),
                "name": "Blast",
                "field": "unlink_book",
                "book_isbn13": "9781681375687",
            },
            {
                "id": str(alert_id),
                "name": "Blast",
                "field": "isbn",
                "before": "9780000000000",
                "after": "9781111111111",
            },
        ]
        with pytest.raises(ValueError, match="périmé"):
            async with maker() as session, session.begin():
                await apply_plan(session, plan, apply=True)

        async with maker() as session:
            remaining = await BookRepository(session).get_all_books_for_alert(alert_id)
            assert len(remaining) == 3
            assert (await session.get(Alert, alert_id)).isbn == "9782756080642"


async def test_an_entry_already_applied_is_not_a_stale_plan():
    """`before` dépassé mais `after` déjà en place : idempotence, pas péremption."""
    async with postgres_replayed_database() as database:
        maker = database.maker
        alert_id = await _seed(maker, name="Batman — Dark Victory", isbn=None)

        plan = [
            {
                "id": str(alert_id),
                "name": "Batman — Dark Victory",
                "field": "isbn",
                "before": "9782365770156",
                "after": None,
            }
        ]
        async with maker() as session, session.begin():
            results = await apply_plan(session, plan, apply=True)
        assert results[0]["status"] == "déjà appliqué"


async def test_the_inverse_plan_restores_every_field_correction():
    async with postgres_replayed_database() as database:
        maker = database.maker
        alert_id = await _seed(maker, excludes=["satrapi", "marjane"])
        forward = [
            {
                "id": str(alert_id),
                "name": "Le Château des animaux",
                "field": "exclude_keywords",
                "before": ["satrapi", "marjane"],
                "after": [],
            }
        ]
        backward = [
            {
                "id": str(alert_id),
                "name": "Le Château des animaux",
                "field": "exclude_keywords",
                "before": [],
                "after": ["satrapi", "marjane"],
            }
        ]

        async with maker() as session, session.begin():
            await apply_plan(session, forward, apply=True)

        async with maker() as session:
            alert = await session.scalar(select(Alert).where(Alert.id == alert_id))
            assert alert is not None
            assert alert.exclude_keywords == []

        async with maker() as session, session.begin():
            result = await apply_plan(session, backward, apply=True)

        assert result[0]["status"] == "corrigé"

        async with maker() as session:
            alert = await session.scalar(select(Alert).where(Alert.id == alert_id))
            assert alert is not None
            assert alert.exclude_keywords == ["satrapi", "marjane"]


async def test_the_real_plan_is_atomic_when_one_alert_has_drifted():
    """Le vrai plan de 16 entrées : si UNE alerte a bougé, rien ne doit être écrit.

    Le lot réel commence par 4 dissociations (Château, puis Blast ×4) avant la
    moindre correction de champ. On fait dériver la dernière entrée — l'alerte
    Hysteria — pour vérifier qu'aucune des 15 précédentes ne subsiste.
    """
    from scripts.apply_audit_plan import _validate_entry

    plan = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "Blast",
            "field": "unlink_book",
            "book_isbn13": "9780613139045",
        },
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "Blast",
            "field": "unlink_book",
            "book_isbn13": "9781681375687",
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "name": "Hysteria",
            "field": "exclude_keywords",
            "before": ["def leppard", "muse"],
            "after": ["def leppard", "muse", "sous-bocks"],
        },
    ]
    for entry in plan:
        _validate_entry(entry)

    async with postgres_replayed_database() as database:
        maker = database.maker
        blast_id = await _seed(maker, name="Blast", isbn="9782756080642")
        await _link(maker, blast_id, "9782205063974", "Blast — Tome 1")
        await _link(maker, blast_id, "9780613139045", "Blast")
        await _link(maker, blast_id, "9781681375687", "Blast off")

        hysteria_id = await _seed(maker, name="Hysteria", excludes=["def leppard", "muse"])
        plan[0]["id"] = str(blast_id)
        plan[1]["id"] = str(blast_id)
        plan[2]["id"] = str(hysteria_id)

        # Hysteria a dérivé : ses exclusions ne correspondent plus au plan.
        async with maker() as session, session.begin():
            alert = await session.get(Alert, hysteria_id)
            alert.exclude_keywords = ["def leppard", "muse", "hysteria magazine"]

        with pytest.raises(ValueError, match="périmé"):
            async with maker() as session, session.begin():
                await apply_plan(session, plan, apply=True)

        async with maker() as session:
            remaining = await BookRepository(session).get_all_books_for_alert(blast_id)
            assert len(remaining) == 3, "les dissociations de Blast ont survécu"
            hysteria = await session.get(Alert, hysteria_id)
            assert hysteria.exclude_keywords == ["def leppard", "muse", "hysteria magazine"]
