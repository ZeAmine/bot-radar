"""La requête des cotes figées, exécutée pour de vrai.

Les tests unitaires de `market_health` mockent le repository : ils prouvent que le
message est bien écrit, jamais que la requête renvoie des lignes. C'est exactement
le trou qui a laissé passer une faute invisible à l'œil — un `HAVING` cumulait deux
conditions qui sont des alternatives, si bien que la requête ne pouvait plus rien
renvoyer. Aucun test ne l'avait vu parce qu'aucun ne l'avait exécutée.

Ces tests jouent donc des lignes. Ils utilisent SQLite : la lecture du `raw_payload`
passe par le comparateur JSON de SQLAlchemy, qui rend `JSON_EXTRACT` là où
PostgreSQL rend `->>`. Le comportement testé est le même des deux côtés.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.constants import AlertStatus, CategorySlug
from app.db.base import Base
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.db.models.user import User
from app.db.repositories.market_price_repo import MarketPriceRepository

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _book_with_cached_low(
    session: AsyncSession,
    *,
    title: str,
    low_cents: int | None,
    alert_status: str = AlertStatus.ACTIVE,
    category_slug: str = CategorySlug.BD_COMICS.value,
    linked: bool = True,
) -> uuid.UUID:
    user = User(telegram_user_id=uuid.uuid4().int % 10**9, is_allowed=True)
    session.add(user)
    await session.flush()

    category = await session.scalar(select(Category).where(Category.slug == category_slug))
    if category is None:
        category = Category(slug=category_slug, label="cat", emoji="x", is_active=True)
        session.add(category)
        await session.flush()

    alert = Alert(
        user_id=user.id,
        category_id=category.id,
        name=f"alerte {title}",
        include_keywords=[title],
        status=AlertStatus(alert_status),
    )
    book = Book(title=title, normalized_title=title.lower(), isbn13=f"978{title[:8]:0<8}")
    session.add_all([alert, book])
    await session.flush()

    if linked:
        session.add(AlertBook(alert_id=alert.id, book_id=book.id))
    session.add(
        BookMarketCache(
            book_id=book.id,
            lowest_delivered_price_cents=low_cents or 0,
            historical_low_1y_cents=low_cents,
        )
    )
    await session.commit()
    return book.id


async def _observe(
    session: AsyncSession,
    book_id: uuid.UUID,
    *,
    days_ago: int,
    certified: bool,
) -> None:
    payload: dict[str, Any] = {"offers": []}
    if certified:
        payload["historical_low_1y_cents"] = 1357
    session.add(
        MarketPriceSnapshot(
            book_id=book_id,
            provider_name="ebay",
            item_price_cents=1500,
            delivered_price_cents=1700,
            source_url=f"https://exemple.test/{book_id}",
            checked_at=datetime.now(UTC) - timedelta(days=days_ago),
            raw_payload=payload,
        )
    )
    await session.commit()


async def test_an_old_observation_is_reported(session: AsyncSession) -> None:
    """Le cas de prod : une cote observée il y a trois semaines, jamais depuis."""
    book_id = await _book_with_cached_low(session, title="Dead Train", low_cents=1357)
    await _observe(session, book_id, days_ago=23, certified=True)

    rows = await MarketPriceRepository(session).find_stale_certified_lows(
        stale_after=datetime.now(UTC) - timedelta(days=7)
    )

    assert [(title, low) for title, _, low, _ in rows] == [("Dead Train", 1357)]
    assert rows[0][3] is not None, "l'âge de la dernière observation doit être connu"


async def test_a_never_observed_book_is_reported(session: AsyncSession) -> None:
    """Jamais observé est un cas distinct : un relevé sans cote 1 an ne compte pas."""
    book_id = await _book_with_cached_low(session, title="Carcajou", low_cents=390)
    await _observe(session, book_id, days_ago=23, certified=False)

    rows = await MarketPriceRepository(session).find_stale_certified_lows(
        stale_after=datetime.now(UTC) - timedelta(days=7)
    )

    assert [(title, last_seen) for title, _, _, last_seen in rows] == [("Carcajou", None)]


async def test_a_fresh_observation_is_not_reported(session: AsyncSession) -> None:
    """Le cas courant : nourri hier, rien à dire. Le signal ne doit pas crier."""
    book_id = await _book_with_cached_low(session, title="Tintin", low_cents=900)
    await _observe(session, book_id, days_ago=1, certified=True)

    rows = await MarketPriceRepository(session).find_stale_certified_lows(
        stale_after=datetime.now(UTC) - timedelta(days=7)
    )

    assert rows == []


async def test_a_book_without_displayed_low_is_ignored(session: AsyncSession) -> None:
    """Sans cote affichée, il n'y a rien de figé à annoncer."""
    await _book_with_cached_low(session, title="Lucky Luke", low_cents=None)

    rows = await MarketPriceRepository(session).find_stale_certified_lows(
        stale_after=datetime.now(UTC) - timedelta(days=7)
    )

    assert rows == []


async def test_a_paused_alert_is_not_reported(session: AsyncSession) -> None:
    """Une alerte en pause n'a pas d'affichage à corriger."""
    book_id = await _book_with_cached_low(
        session, title="Asterix", low_cents=1200, alert_status=AlertStatus.PAUSED
    )
    await _observe(session, book_id, days_ago=23, certified=True)

    rows = await MarketPriceRepository(session).find_stale_certified_lows(
        stale_after=datetime.now(UTC) - timedelta(days=7)
    )

    assert rows == []


async def test_a_book_outside_bd_is_not_reported(session: AsyncSession) -> None:
    """Le contrôle est celui des BD : une cote de jeu n'a pas la même référence."""
    book_id = await _book_with_cached_low(
        session, title="Zelda", low_cents=4500, category_slug=CategorySlug.GAMING.value
    )
    await _observe(session, book_id, days_ago=23, certified=True)

    rows = await MarketPriceRepository(session).find_stale_certified_lows(
        stale_after=datetime.now(UTC) - timedelta(days=7)
    )

    assert rows == []


async def test_the_oldest_books_come_first_and_the_list_is_bounded(session: AsyncSession) -> None:
    """Une liste bornée doit rester celle qui inquiète le plus."""
    for index in range(12):
        book_id = await _book_with_cached_low(
            session, title=f"Book {index:02d}", low_cents=1000 + index
        )
        await _observe(session, book_id, days_ago=40 - index, certified=True)

    rows = await MarketPriceRepository(session).find_stale_certified_lows(
        stale_after=datetime.now(UTC) - timedelta(days=7), limit=8
    )

    assert len(rows) == 8
    assert [title for title, _, _, _ in rows] == [f"Book {i:02d}" for i in range(8)]


async def test_a_book_linked_to_no_alert_is_not_reported(session: AsyncSession) -> None:
    """Sans alerte, une cote n'est affichée nulle part."""
    book_id = await _book_with_cached_low(
        session, title="Orphelin", low_cents=700, linked=False
    )
    await _observe(session, book_id, days_ago=23, certified=True)

    rows = await MarketPriceRepository(session).find_stale_certified_lows(
        stale_after=datetime.now(UTC) - timedelta(days=7)
    )

    assert rows == []