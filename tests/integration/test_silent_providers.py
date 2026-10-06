"""La requête des sources muettes, exécutée pour de vrai.

Le contrôle de santé des fournisseurs repose sur un regroupement avec `HAVING`
et un compteur `FILTER` : deux constructions que la lecture seule ne valide pas
(cf. les cotes figées, laissées invisibles par un `HAVING` incorrect). Ces
tests jouent des relevés pour de vrai, sur SQLite comme en production le fera
sur PostgreSQL.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.book import Book
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.db.repositories.market_price_repo import MarketPriceRepository
from tests.database import postgres_replayed_database

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    async with postgres_replayed_database() as database:
        session = database.session
        yield session


async def _observe(
    session: AsyncSession,
    book_id: uuid.UUID,
    *,
    provider: str,
    hours_ago: float,
) -> None:
    payload: dict[str, Any] = {"offers": []}
    session.add(
        MarketPriceSnapshot(
            book_id=book_id,
            provider_name=provider,
            item_price_cents=1500,
            delivered_price_cents=1700,
            source_url=f"https://exemple.test/{book_id}",
            checked_at=datetime.now(UTC) - timedelta(hours=hours_ago),
            raw_payload=payload,
        )
    )
    await session.commit()


async def _book(session: AsyncSession) -> uuid.UUID:
    book = Book(
        title="Dead Train",
        normalized_title="dead train",
        isbn13="9782380463972",
    )
    session.add(book)
    await session.flush()
    return book.id


async def test_a_recently_active_silent_provider_is_reported(session: AsyncSession) -> None:
    """Le cas de prod : trois relevés cette semaine, plus rien depuis 50 h."""
    book_id = await _book(session)
    for hours in (24 * 6, 24 * 5, 50):
        await _observe(session, book_id, provider="momox", hours_ago=hours)

    rows = await MarketPriceRepository(session).find_silent_providers(
        silent_after=datetime.now(UTC) - timedelta(hours=48),
        recent_after=datetime.now(UTC) - timedelta(days=7),
    )

    assert [(name,) for name, _ in rows] == [("momox",)]
    assert rows[0][1] is not None, "le dernier relevé doit être connu"


async def test_a_provider_that_answered_recently_is_not_reported(session: AsyncSession) -> None:
    """Le cas courant : la source répond, rien à dire."""
    book_id = await _book(session)
    for hours in (50, 30, 5):
        await _observe(session, book_id, provider="momox", hours_ago=hours)

    rows = await MarketPriceRepository(session).find_silent_providers(
        silent_after=datetime.now(UTC) - timedelta(hours=48),
        recent_after=datetime.now(UTC) - timedelta(days=7),
    )

    assert rows == []


async def test_a_provider_rarely_solicited_is_not_reported(session: AsyncSession) -> None:
    """Un unique relevé ne dessine pas une habitude : le silence n'est pas un incident."""
    book_id = await _book(session)
    await _observe(session, book_id, provider="gibert", hours_ago=50)

    rows = await MarketPriceRepository(session).find_silent_providers(
        silent_after=datetime.now(UTC) - timedelta(hours=48),
        recent_after=datetime.now(UTC) - timedelta(days=7),
        min_recent_observations=3,
    )

    assert rows == []


async def test_a_long_decommissioned_provider_stops_alerting(session: AsyncSession) -> None:
    """Un fournisseur retiré depuis deux semaines n'est plus sollicité : fini."""
    book_id = await _book(session)
    for days in (14, 15, 16):
        await _observe(
            session, book_id, provider="recyclivre", hours_ago=days * 24
        )

    rows = await MarketPriceRepository(session).find_silent_providers(
        silent_after=datetime.now(UTC) - timedelta(hours=48),
        recent_after=datetime.now(UTC) - timedelta(days=7),
        min_recent_observations=3,
    )

    assert rows == []


async def test_each_provider_is_judged_on_its_own(session: AsyncSession) -> None:
    """Une source muette n'innocente pas les autres, ni ne les condamne avec elle."""
    book_id = await _book(session)
    for hours in (24 * 6, 24 * 5, 49):
        await _observe(session, book_id, provider="momox", hours_ago=hours)
    for hours in (24 * 3, 6):
        await _observe(session, book_id, provider="gibert", hours_ago=hours)

    rows = await MarketPriceRepository(session).find_silent_providers(
        silent_after=datetime.now(UTC) - timedelta(hours=48),
        recent_after=datetime.now(UTC) - timedelta(days=7),
    )

    assert [(name,) for name, _ in rows] == [("momox",)]
