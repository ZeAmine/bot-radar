"""Fenêtre glissante documentée ; aucune promotion d'un ancien cache en historique."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.db.repositories.market_price_repo import MarketPriceRepository, _utc
from app.domain.enums import ComparisonStatus
from app.providers.base import MarketOffer
from app.services import market_price_service as market
from app.services.book_market_pricing import BookPricingResult, persist_market_result
from app.services.market_price_service import admissible_reference_price
from tests.database import postgres_replayed_database


def test_unproven_legacy_minimum_never_overrides_a_current_offer():
    now = datetime.now(UTC)
    cache = BookMarketCache(
        historical_low_1y_cents=100,
        historical_low_at=now - timedelta(days=500),
        lowest_delivered_price_cents=1000,
        checked_at=now,
        expires_at=now + timedelta(hours=24),
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    assert admissible_reference_price(cache) == (1000, False)


@pytest.fixture
async def evidence(monkeypatch):
    from app.db.repositories import market_price_repo as repository

    moment = SimpleNamespace(now=datetime(2026, 10, 12, 12, tzinfo=UTC))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.now.astimezone(tz) if tz else moment.now.replace(tzinfo=None)

    monkeypatch.setattr(repository, "datetime", Clock)
    monkeypatch.setattr(market, "datetime", Clock)
    async with postgres_replayed_database() as db:
        book = Book(title="Preuve", normalized_title="preuve")
        db.session.add(book)
        await db.session.flush()
        cache = BookMarketCache(
            book_id=book.id,
            historical_low_1y_cents=1,
            lowest_delivered_price_cents=5000,
            checked_at=moment.now,
            expires_at=moment.now + timedelta(hours=1),
            comparison_status=ComparisonStatus.AVAILABLE,
        )
        db.session.add(cache)
        await db.session.commit()
        yield SimpleNamespace(
            db=db, book=book, cache=cache, clock=moment, repo=MarketPriceRepository(db.session)
        )


def offer(at, price=1000, **values):
    return MarketOffer(
        provider_name="synthetic",
        item_price_cents=price,
        shipping_price_cents=0,
        delivered_price_cents=price,
        source_url="https://example.invalid",
        item_condition="Très bon",
        observed_at=at,
        **values,
    )


@pytest.mark.parametrize(
    ("offset", "accepted"),
    [
        (-1, False),
        (0, False),
        (1, True),
    ],
)
async def test_window_boundary_microseconds(evidence, offset, accepted):
    e = evidence
    at = e.clock.now - timedelta(days=365) + timedelta(microseconds=offset)
    await e.repo.save_snapshot(e.book.id, offer(at))
    await e.repo.refresh_annual_references([e.book.id], now=e.clock.now, force=True)
    await e.db.session.commit()
    async with e.db.maker() as reader:
        cache = (await reader.scalars(select(BookMarketCache))).one()
        assert cache.annual_count == int(accepted)
        assert cache.historical_low_1y_cents == 1
        assert admissible_reference_price(cache) == ((1000, True) if accepted else (5000, False))


async def test_minimum_expires_without_collection_and_ties_are_deterministic(evidence):
    e = evidence
    oldest = e.clock.now - timedelta(days=365) + timedelta(seconds=1)
    first = await e.repo.save_snapshot(e.book.id, offer(oldest, 100))
    second = await e.repo.save_snapshot(e.book.id, offer(e.clock.now - timedelta(days=2), 200))
    third = await e.repo.save_snapshot(e.book.id, offer(e.clock.now - timedelta(days=1), 200))
    await e.repo.refresh_annual_references([e.book.id], now=e.clock.now, force=True)
    assert e.cache.annual_low_snapshot_id == first.id
    await e.db.session.commit()
    e.clock.now += timedelta(seconds=1)
    assert admissible_reference_price(e.cache) == (5000, False)
    await e.repo.refresh_annual_references([e.book.id], now=e.clock.now)
    assert e.cache.annual_low_cents == 200
    assert e.cache.annual_low_snapshot_id == third.id
    assert e.cache.annual_low_provider == "synthetic"
    assert e.cache.annual_count == 2
    assert _utc(e.cache.annual_first_at) == second.observed_at
    before = [first.checked_at, second.checked_at, third.checked_at]
    e.clock.now += timedelta(days=366)
    await e.repo.refresh_annual_references([e.book.id], now=e.clock.now)
    assert e.cache.annual_low_cents is None
    assert admissible_reference_price(e.cache) == (None, False)
    assert [first.checked_at, second.checked_at, third.checked_at] == before


@pytest.mark.parametrize("kind", ["unknown", "future", "aggregate", "new", "inferior", "currency"])
async def test_unusable_evidence_is_excluded(evidence, kind):
    e = evidence
    value = offer(e.clock.now)
    if kind == "unknown":
        value.observed_at = None
    elif kind == "future":
        value.observed_at += timedelta(microseconds=1)
    elif kind == "aggregate":
        value.delivered_price_cents = 0
        value.is_available = False
        value.raw_payload = {"historical_low_1y_cents": 10}
    elif kind == "new":
        value.item_condition = "Neuf"
    elif kind == "inferior":
        value.item_condition = "Bon"
    else:
        value.currency = "USD"
    await e.repo.save_snapshot(e.book.id, value)
    await e.repo.refresh_annual_references([e.book.id], now=e.clock.now, force=True)
    assert e.cache.annual_count == 0
    assert admissible_reference_price(e.cache) == (5000, False)


async def test_retry_and_rollback_never_redate_or_duplicate_observation(evidence):
    e = evidence
    value = offer(e.clock.now - timedelta(hours=25))
    value_book_id = e.book.id
    result = BookPricingResult.from_offers([value], value, None)
    await persist_market_result(e.db.session, e.book.id, result)
    await e.db.session.rollback()
    await persist_market_result(e.db.session, value_book_id, result)
    await e.db.session.commit()
    async with e.db.maker() as reader:
        rows = list((await reader.scalars(select(MarketPriceSnapshot))).all())
        assert len(rows) == 1
        assert _utc(rows[0].observed_at) == value.observed_at
    await persist_market_result(e.db.session, value_book_id, result)
    await e.db.session.commit()
    async with e.db.maker() as reader:
        rows = list((await reader.scalars(select(MarketPriceSnapshot))).all())
        cache = (await reader.scalars(select(BookMarketCache))).one()
        assert len(rows) == 1
        assert cache.annual_count == 1
        assert _utc(cache.checked_at) == value.observed_at
        assert _utc(cache.expires_at) == value.observed_at + timedelta(hours=24)


async def test_legacy_snapshot_keeps_its_original_timestamp(evidence):
    e = evidence
    at = e.clock.now - timedelta(days=80)
    row = MarketPriceSnapshot(
        book_id=e.book.id,
        provider_name="old",
        item_price_cents=200,
        delivered_price_cents=200,
        shipping_price_cents=0,
        currency="EUR",
        source_url="https://example.invalid",
        checked_at=at,
        is_comparable=True,
        item_condition="Très bon",
        raw_payload={"historical_low_1y_cents": 1},
    )
    e.db.session.add(row)
    await e.db.session.commit()
    await e.repo.refresh_annual_references([e.book.id], now=e.clock.now)
    assert e.cache.annual_low_cents == 200
    assert _utc(e.cache.annual_low_observed_at) == at
    assert row.checked_at == at
    assert row.observed_at is None
    assert e.cache.historical_low_1y_cents == 1


@pytest.mark.parametrize(
    "now",
    [
        datetime(2024, 2, 29, 12, tzinfo=UTC),
        datetime(2026, 3, 29, 2, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 2, 30, tzinfo=UTC),
    ],
)
async def test_window_uses_elapsed_days_not_calendar_year(evidence, now):
    from zoneinfo import ZoneInfo

    e = evidence
    e.clock.now = now
    at = (now - timedelta(days=365) + timedelta(microseconds=1)).astimezone(
        ZoneInfo("Europe/Paris")
    )
    await e.repo.save_snapshot(e.book.id, offer(at, 900))
    await e.repo.refresh_annual_references([e.book.id], now=now, force=True)
    assert admissible_reference_price(e.cache) == (900, True)
    assert _utc(e.cache.annual_low_observed_at) == at.astimezone(UTC)


async def test_same_date_tie_uses_snapshot_identifier(evidence):
    import uuid

    e = evidence
    values = [offer(e.clock.now, 100), offer(e.clock.now, 100)]
    values[0].observation_id = uuid.UUID("a0000000-0000-0000-0000-000000000002")
    values[1].observation_id = uuid.UUID("a0000000-0000-0000-0000-000000000001")
    for value in values:
        await e.repo.save_snapshot(e.book.id, value)
    await e.repo.refresh_annual_references([e.book.id], now=e.clock.now, force=True)
    assert e.cache.annual_low_snapshot_id == uuid.UUID("a0000000-0000-0000-0000-000000000001")
    assert e.cache.annual_count == 2


async def test_expired_projection_is_not_used_if_recalculation_fails(evidence, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    e = evidence
    await e.repo.save_snapshot(
        e.book.id, offer(e.clock.now - timedelta(days=365) + timedelta(seconds=1))
    )
    await e.repo.refresh_annual_references([e.book.id], now=e.clock.now, force=True)
    await e.db.session.commit()
    e.clock.now += timedelta(seconds=1)

    async def fail(*_args, **_kwargs):
        raise SQLAlchemyError("synthetic annual query failure")

    monkeypatch.setattr(e.db.session, "execute", fail)
    with pytest.raises(SQLAlchemyError, match="synthetic annual query"):
        await e.repo.refresh_annual_references([e.book.id], now=e.clock.now)
    assert admissible_reference_price(e.cache) == (5000, False)


async def test_read_recalculates_locally_without_writing_or_redating(evidence):
    from app.db.repositories.book_repo import BookRepository

    e = evidence
    at = e.clock.now - timedelta(days=365) + timedelta(seconds=1)
    await e.repo.save_snapshot(e.book.id, offer(at, 100))
    await e.repo.save_snapshot(e.book.id, offer(e.clock.now, 200))
    await e.repo.refresh_annual_references([e.book.id], now=e.clock.now, force=True)
    await e.db.session.commit()
    old_checked = e.cache.checked_at
    e.clock.now += timedelta(seconds=1)
    async with e.db.maker() as reader:
        book = await BookRepository(reader).get_by_id(e.book.id)
        assert admissible_reference_price(book.market_cache) == (200, True)
        assert _utc(book.market_cache.checked_at) == old_checked
        assert not reader.dirty
    async with e.db.maker() as reader:
        cache = (await reader.scalars(select(BookMarketCache))).one()
        assert cache.annual_low_cents == 100  # Projection SQL expirée : pas d'écriture de lecture.
        assert admissible_reference_price(cache) == (5000, False)


async def test_financial_retention_and_payload_retention_are_distinct(evidence, monkeypatch):
    from app.db.models.alert import Alert
    from app.db.models.alert_book import AlertBook
    from app.db.models.bot_event import BotEvent
    from app.db.models.category import Category
    from app.db.models.user import User
    from app.tasks import cleanup

    e = evidence
    user = User(telegram_user_id=424242, is_allowed=True)
    category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
    e.db.session.add_all([user, category])
    await e.db.session.flush()
    alert = Alert(
        user_id=user.id,
        category_id=category.id,
        name="Preuve",
        include_keywords=["preuve"],
        status="ACTIVE",
    )
    e.db.session.add(alert)
    await e.db.session.flush()
    e.db.session.add(AlertBook(alert_id=alert.id, book_id=e.book.id))
    ids = []
    for days, comparable in [(91, True), (365, True), (366, True), (367, True), (91, False)]:
        at = e.clock.now - timedelta(days=days)
        row = await e.repo.save_snapshot(
            e.book.id, offer(at, days, is_comparable=comparable, raw_payload={"synthetic": "body"})
        )
        ids.append(row.id)
    e.cache.checked_at = e.clock.now - timedelta(days=100)
    e.cache.current_offers = [{"delivered_price_cents": 5000, "raw_payload": {"fake": "body"}}]
    event = BotEvent(event_type="synthetic", created_at=e.clock.now - timedelta(days=91))
    e.db.session.add(event)
    await e.db.session.commit()
    monkeypatch.setattr(cleanup, "AsyncSessionLocal", e.db.maker)
    monkeypatch.setattr(cleanup, "datetime", market.datetime)
    await cleanup.cleanup_old_data_task()
    async with e.db.maker() as reader:
        rows = list((await reader.scalars(select(MarketPriceSnapshot))).all())
        assert {row.id for row in rows} == set(ids[:3])
        assert all(row.raw_payload is None for row in rows)
        assert all(row.provider_name == "synthetic" for row in rows)
        assert all(row.item_condition == "Très bon" for row in rows)
        assert list((await reader.scalars(select(BotEvent))).all()) == []
        cache = (await reader.scalars(select(BookMarketCache))).one()
        assert cache.current_offers == [{"delivered_price_cents": 5000}]
        assert cache.annual_count == 1  # 365 et 366 jours conservés, mais hors fenêtre.
        assert cache.annual_low_cents == 91


async def test_an_observation_identifier_cannot_be_reused_for_a_different_price(evidence):
    e = evidence
    value = offer(e.clock.now, 100)
    await e.repo.save_snapshot(e.book.id, value)
    await e.db.session.commit()
    value.delivered_price_cents = 200
    with pytest.raises(ValueError, match="Identité"):
        await e.repo.save_snapshot(e.book.id, value)
    async with e.db.maker() as reader:
        rows = list((await reader.scalars(select(MarketPriceSnapshot))).all())
        assert len(rows) == 1
        assert rows[0].delivered_price_cents == 100


def test_mixed_lot_labels_are_shared_by_summary_and_panel():
    from app.services.tome_extractor import TomeMatchResult

    now = datetime.now(UTC)
    books = [Book(title=f"Preuve Tome {n}") for n in (1, 2)]
    books[0].market_cache = BookMarketCache(
        annual_low_cents=100,
        annual_low_provider="synthetic",
        annual_low_snapshot_id=uuid.uuid4(),
        annual_low_observed_at=now,
        annual_first_at=now,
        annual_last_at=now,
        annual_count=1,
        annual_computed_at=now,
        annual_expires_at=now + timedelta(days=365),
    )
    books[1].market_cache = BookMarketCache(
        lowest_delivered_price_cents=200,
        checked_at=now,
        expires_at=now + timedelta(hours=24),
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    listing = SimpleNamespace(title="Lot Preuve", estimated_total_cents=50, item_price_cents=10)
    tome = TomeMatchResult(is_lot=True, lot_tomes=[1, 2], confidence="high")
    summary = market.MarketPriceService.get_comparison_summary(listing, None, books, tome)
    panel = market.MarketPriceService.format_market_details_card(listing, None, books, tome)
    # Libellé compact restauré : le lot annonce son total, sans suffixe de dates.
    assert "<b>Prix du marché (Lot) :</b> ~3,00 €" in summary
    assert "<b>Total du lot :</b> ~3,00 €" in panel
    assert "3,00" in summary
    assert "3,00" in panel


def test_an_expired_known_offer_is_not_announced_as_an_empty_collection():
    now = datetime.now(UTC)
    book = Book(title='Preuve')
    book.market_cache = BookMarketCache(lowest_delivered_price_cents=1000,
        checked_at=now-timedelta(days=30), expires_at=now-timedelta(days=29),
        comparison_status=ComparisonStatus.AVAILABLE)
    line = market.cote_absence_line(book)
    assert 'expiré' in line
    assert 'Aucune offre' not in line
    assert not market.has_usable_price(book.market_cache)


async def test_historical_points_enter_annual_minimum_but_not_current_offers(evidence):
    point = offer(evidence.clock.now - timedelta(days=60), 545,
                  is_available=False, is_historical=True)
    current = offer(evidence.clock.now, 1103)
    result = BookPricingResult.from_offers([current, point], current, None)
    cache = await persist_market_result(evidence.db.session, evidence.book.id, result)
    await evidence.db.session.commit()
    assert cache.annual_low_cents == 545
    assert cache.lowest_delivered_price_cents == 1103
    assert len(cache.current_offers) == 1
    latest = await evidence.repo.get_latest_snapshot(evidence.book.id)
    assert latest.delivered_price_cents == 1103
    assert admissible_reference_price(cache) == (545, True)
    from app.db.repositories.book_repo import BookRepository
    book = await BookRepository(evidence.db.session).get_by_id(evidence.book.id)
    text = market.MarketPriceService.format_market_details_card(listing=None, book=book)
    assert "Ancien minimum" not in text
    assert text.count("Minimum sur 1 an") == 1
    assert "5,45" in text


def test_market_card_omits_unproven_legacy_minimum():
    now = datetime.now(UTC)
    book = Book(title="Grimr", normalized_title="grimr")
    book.market_cache = BookMarketCache(
        historical_low_1y_cents=545, lowest_delivered_price_cents=1154,
        checked_at=now, expires_at=now+timedelta(hours=24),
        comparison_status=ComparisonStatus.AVAILABLE,
        annual_low_cents=1103, annual_low_observed_at=now,
        annual_computed_at=now, annual_expires_at=now+timedelta(days=30),
        annual_count=1, annual_first_at=now, annual_last_at=now,
        annual_low_snapshot_id=uuid.uuid4(), annual_low_provider="synthetic",
    )
    text = market.MarketPriceService.format_market_details_card(listing=None, book=book)
    assert "Ancien minimum" not in text
    assert text.count("Minimum sur 1 an") == 1
    assert "11,03" in text


async def test_history_only_with_failed_offers_does_not_fake_current_freshness(evidence):
    from app.providers.base import MarketOffers

    evidence.book.created_at = evidence.clock.now-timedelta(days=3)
    await evidence.db.session.flush()
    historical = MarketOffer(
        provider_name="chasse_aux_livres", item_price_cents=0,
        shipping_price_cents=None, delivered_price_cents=0,
        source_url="https://example.invalid/book", is_available=False,
        raw_payload={"historical_low_1y_cents": 545},
    )
    offers = MarketOffers([historical], failed_providers=("synthetic",), historical_checked=True)
    result = BookPricingResult.from_offers(offers, None, 545)
    from dataclasses import replace
    result = replace(result, collected_at=evidence.clock.now)
    cache = await persist_market_result(evidence.db.session, evidence.book.id, result)
    await evidence.db.session.commit()
    assert cache.last_successful_collection_at is None
    assert cache.lowest_delivered_price_cents == 5000  # Dernier prix connu conservé.
    rows, total = await evidence.repo.find_stale_current_collections(
        stale_after=evidence.clock.now-timedelta(hours=48),
    )
    # Le livre de cette fixture n'est associé à aucune alerte : la requête ne le surveille pas.
    assert rows == []
    assert total == 0
