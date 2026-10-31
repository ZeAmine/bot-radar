import asyncio
import os
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Alert, AlertBook, Book, BookMarketCache, Category, User
from app.db.repositories.book_repo import BookRepository
from app.providers.base import CollectionAttempt, MarketOffer, MarketOffers
from app.services.book_market_pricing import (
    BookPricingResult,
    _reserve_next_refresh,
    persist_market_result,
)
from app.services.market_refresh_policy import aware
from tests.database import postgres_replayed_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
        reason="Réservations concurrentes sur PostgreSQL isolé requises",
    ),
]


async def seed(session, slots, *, due_at):
    assert session.get_bind().dialect.name == "postgresql"
    user = User(telegram_user_id=424242, is_allowed=True)
    category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
    session.add_all([user, category])
    await session.flush()
    books = []
    for index, slot in enumerate(slots):
        book = Book(title=f"Livre {index}", normalized_title=f"livre {index}")
        alert = Alert(
            user_id=user.id,
            category_id=category.id,
            name=book.title,
            include_keywords=["livre"],
        )
        session.add_all([book, alert])
        await session.flush()
        session.add(AlertBook(alert_id=alert.id, book_id=book.id))
        session.add(BookMarketCache(
            book_id=book.id,
            next_refresh_at=slot,
            collection_status="complete",
            refresh_state={
                component: {"due_at": due_at.isoformat(), "failures": 0}
                for component in ("current", "history")
            },
        ))
        books.append(book)
    await session.commit()
    return books


async def cache_for(session, book_id):
    return await session.scalar(
        select(BookMarketCache).where(BookMarketCache.book_id == book_id)
    )


async def test_concurrent_independent_reservations_serialize_until_commit():
    """Deux réservations concurrentes se sérialisent ; la seconde attend le commit.

    L'ancienne version vérifiait que huit livres tenaient dans un créneau commun.
    Il n'y a plus de créneaux à remplir : ce qui reste à prouver est la réservation
    elle-même — la session qui la détient garde le verrou jusqu'à la fin de sa
    transaction, et elle ne réécrit l'échéance d'aucun autre livre.
    """
    now = datetime.now(UTC)
    slot = (now + timedelta(minutes=30)).replace(microsecond=0)
    async with postgres_replayed_database() as db:
        books = await seed(db.session, [slot] * 7 + [None, None], due_at=slot)
        first_ready = asyncio.Event()
        release_first = asyncio.Event()

        async def first():
            async with db.maker() as session:
                cache = await cache_for(session, books[7].id)
                await _reserve_next_refresh(session, books[7].id, cache)
                first_ready.set()
                await release_first.wait()
                await session.commit()

        async def second():
            await first_ready.wait()
            async with db.maker() as session:
                cache = await cache_for(session, books[8].id)
                await _reserve_next_refresh(session, books[8].id, cache)
                await session.commit()

        participants = [asyncio.create_task(first()), asyncio.create_task(second())]
        try:
            await asyncio.wait_for(first_ready.wait(), timeout=3)
            await asyncio.wait({participants[1]}, timeout=0.25)
            assert not participants[1].done(), (
                "la seconde réservation doit attendre le commit de la première"
            )
            release_first.set()
            await asyncio.wait_for(asyncio.gather(*participants), timeout=5)
        finally:
            release_first.set()
            for participant in participants:
                if not participant.done():
                    participant.cancel()
            await asyncio.wait_for(
                asyncio.gather(*participants, return_exceptions=True), timeout=3
            )
        async with db.maker() as reader:
            slots = list((await reader.scalars(select(BookMarketCache.next_refresh_at))).all())
            assert all(value is not None for value in slots)
            # Les sept livres déjà datés gardent leur échéance : réserver ne
            # redistribue pas une file que personne n'a demandée.
            occupancy = Counter(aware(value) for value in slots)
            assert occupancy[slot] >= 7, dict(occupancy)


@pytest.mark.parametrize("report_active", [False, True], ids=["expired", "active"])
async def test_history_only_after_administrative_report_keeps_a_valid_reservation(report_active):
    now = datetime.now(UTC)
    retry = now + timedelta(hours=2, minutes=7) if report_active else now - timedelta(hours=1)
    async with postgres_replayed_database() as db:
        book, = await seed(db.session, [retry], due_at=retry)
        cache = await cache_for(db.session, book.id)
        cache.collection_status = "deferred"
        cache.last_attempt_at = now - timedelta(hours=3)
        await db.session.commit()
        original_attempt = cache.last_attempt_at
        point = MarketOffer(
            "chasse_aux_livres", 900, None, 900, "https://example.com/history",
            item_condition="Très bon état +",
            is_historical=True,
            is_available=False,
            price_includes_shipping=True,
            observed_at=now - timedelta(days=1),
        )
        offers = MarketOffers(
            [point],
            historical_checked=True,
            attempts=(
                CollectionAttempt("chasse_aux_livres", "current", "skipped", "not_requested"),
                CollectionAttempt("chasse_aux_livres", "history", "success"),
            ),
        )
        result = BookPricingResult.from_offers(offers, None, 900)
        await persist_market_result(db.session, book.id, result)
        await db.session.commit()
        async with db.maker() as reader:
            written = await cache_for(reader, book.id)
            assert written.historical_checked_at is not None
            assert written.source_history["price_cents"] == 900
            assert written.last_successful_collection_at is None
            assert aware(written.last_attempt_at) == aware(original_attempt)
            assert written.next_refresh_at is not None
            if report_active:
                assert aware(written.next_refresh_at) == retry
            else:
                # Plus de grille : quand le report a expiré, l'échéance repart du
                # modèle de fraîcheur — la cote est due depuis ``retry`` — et non
                # d'un créneau horaire à retrouver.
                assert aware(written.next_refresh_at) > now
                assert aware(written.next_refresh_at) != retry


async def test_targeted_reservation_preserves_another_books_administrative_deadline():
    now = datetime.now(UTC)
    retry = (now + timedelta(hours=2)).replace(minute=17, second=23, microsecond=456789)
    async with postgres_replayed_database() as db:
        target, other = await seed(db.session, [None, retry], due_at=now)
        other_cache = await cache_for(db.session, other.id)
        other_cache.collection_status = "deferred"
        other_cache.refresh_state = {
            component: {"due_at": retry.isoformat(), "failures": 0}
            for component in ("current", "history")
        }
        await db.session.commit()
        async with db.maker() as writer:
            await BookRepository(writer).plan_price_refreshes(book_ids={target.id})
            await writer.commit()
        async with db.maker() as reader:
            unchanged = await cache_for(reader, other.id)
            planned = await cache_for(reader, target.id)
            assert unchanged.collection_status == "deferred"
            assert aware(unchanged.next_refresh_at) == retry
            assert unchanged.refresh_state == {
                component: {"due_at": retry.isoformat(), "failures": 0}
                for component in ("current", "history")
            }
            assert planned.next_refresh_at is not None


@pytest.mark.parametrize("ending", ["rollback", "close", "cancel"])
async def test_transaction_end_releases_reservation_for_another_session(ending):
    now = datetime.now(UTC)
    async with postgres_replayed_database() as db:
        first_book, second_book = await seed(db.session, [None, None], due_at=now)
        ready = asyncio.Event()
        hold = asyncio.Event()

        async def first():
            async with db.maker() as session:
                cache = await cache_for(session, first_book.id)
                await _reserve_next_refresh(session, first_book.id, cache)
                ready.set()
                if ending == "rollback":
                    await session.rollback()
                elif ending == "cancel":
                    await hold.wait()

        participant = asyncio.create_task(first())
        try:
            await asyncio.wait_for(ready.wait(), timeout=3)
            if ending == "cancel":
                participant.cancel()
            await asyncio.wait_for(
                asyncio.gather(participant, return_exceptions=True), timeout=3,
            )

            async def second():
                async with db.maker() as session:
                    cache = await cache_for(session, second_book.id)
                    await _reserve_next_refresh(session, second_book.id, cache)
                    await session.commit()

            await asyncio.wait_for(second(), timeout=3)
        finally:
            if not participant.done():
                participant.cancel()
                await asyncio.gather(participant, return_exceptions=True)


async def test_two_reservations_in_the_same_transaction_are_reentrant():
    now = datetime.now(UTC)
    async with postgres_replayed_database() as db:
        books = await seed(db.session, [None, None], due_at=now)

        async def reserve_both():
            async with db.maker() as session:
                for book in books:
                    cache = await cache_for(session, book.id)
                    await _reserve_next_refresh(session, book.id, cache)
                await session.commit()

        await asyncio.wait_for(reserve_both(), timeout=3)
        async with db.maker() as reader:
            for book in books:
                cache = await cache_for(reader, book.id)
                assert cache.next_refresh_at is not None
