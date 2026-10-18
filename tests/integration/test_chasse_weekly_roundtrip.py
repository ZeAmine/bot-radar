import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import CollectionAttempt, MarketOffer, MarketOffers
from app.providers.chasse_aux_livres import parse_historical_observations
from app.services.book_market_pricing import BookPricingResult, persist_market_result
from app.services.market_price_service import MarketPriceService
from tests.database import postgres_replayed_database


async def test_observed_jean_doux_history_is_stored_and_displayed_after_restart(monkeypatch):
    from app.providers import chasse_aux_livres

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = datetime(2026, 10, 18, 12, tzinfo=UTC)
            return moment if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(chasse_aux_livres, "datetime", Clock)
    fixture = Path(__file__).parents[1] / "fixtures/chasse/jean_doux_history_2026_10_18.json"
    data = json.loads(fixture.read_text())
    url = "https://www.chasse-aux-livres.fr/prix/2756079332/9782756079332"
    points = parse_historical_observations(data, url)
    offers = MarketOffers(points, historical_checked=True, attempts=(
        CollectionAttempt("chasse_aux_livres", "current"),
        CollectionAttempt("chasse_aux_livres", "history"),
    ))
    async with postgres_replayed_database() as database:
        book = Book(title="Jean Doux et le mystère de la disquette molle",
                    normalized_title="jean doux", isbn13="9782756079332")
        database.session.add(book)
        await database.session.flush()
        book_id = book.id
        result = BookPricingResult.from_offers(offers, None, None)
        await persist_market_result(database.session, book_id, result)
        await database.session.commit()
        async with database.maker() as reader:
            from app.db.repositories.book_repo import BookRepository

            stored = await BookRepository(reader).get_by_id(book_id)
            cache = stored.market_cache
            assert cache.source_history["price_cents"] == 936
            assert cache.historical_low_1y_cents == 936
            assert cache.historical_low_at == datetime(2026, 7, 2, 22, 53, tzinfo=UTC).replace(
                tzinfo=cache.historical_low_at.tzinfo,
            )
            creation = MarketPriceService.get_comparison_summary(None, stored, for_creation=True)
            panel = MarketPriceService.format_market_details_card(None, stored)
            assert "Prix bas constaté :</b> 9,36 €" in creation
            assert "Prix bas constaté :</b> 9,36 €" in panel
            successful = cache.historical_checked_at
            previous_history = dict(cache.source_history)
        failure = BookPricingResult.from_offers(MarketOffers(
            failed_providers=("chasse_aux_livres",),
            attempts=(CollectionAttempt("chasse_aux_livres", "current", "failed"),
                      CollectionAttempt("chasse_aux_livres", "history", "failed"))), None, None)
        await persist_market_result(database.session, book_id, failure)
        await database.session.commit()
        async with database.maker() as reader:
            cache = (await reader.scalars(select(BookMarketCache))).one()
            assert cache.source_history == previous_history
            assert cache.historical_checked_at == successful


async def test_successful_partial_collection_retains_other_component_and_offers():
    now = datetime.now(UTC)
    async with postgres_replayed_database() as database:
        book = Book(title="Hoka Hey !", normalized_title="hoka hey")
        database.session.add(book)
        await database.session.flush()
        old = now-timedelta(days=8)
        cache = BookMarketCache(book_id=book.id, last_successful_collection_at=old,
            lowest_delivered_price_cents=1200, current_offers=[{
                "provider_name": "retained", "delivered_price_cents": 1200,
                "source_url": "https://example.com/retained", "item_condition": "Très bon",
            }])
        database.session.add(cache)
        await database.session.commit()
        point = MarketOffer("chasse_aux_livres", 1000, None, 1000, "https://example.com/history",
            is_historical=True, is_available=False, price_includes_shipping=True,
            item_condition="Très bon état +", observed_at=now-timedelta(days=2))
        result = BookPricingResult.from_offers(MarketOffers([point], historical_checked=True,
            failed_providers=("retained",), attempts=(
                CollectionAttempt("retained", "current", "failed"),
                CollectionAttempt("chasse_aux_livres", "history"),
            )), None, 1000)
        await persist_market_result(database.session, book.id, result)
        await database.session.commit()
        assert cache.lowest_delivered_price_cents == 1200
        assert cache.current_offers[0]["provider_name"] == "retained"
        assert cache.last_successful_collection_at == old
        assert cache.historical_checked_at == result.collected_at


async def test_backfill_inventory_preserves_unknown_dates_and_does_not_write():
    from app.db.models.alert import Alert
    from app.db.models.alert_book import AlertBook
    from app.db.models.category import Category
    from app.db.models.user import User
    from app.services.market_backfill import inventory_market_backfill, manifest_digest

    async with postgres_replayed_database() as database:
        session = database.session
        user = User(telegram_user_id=8282, is_allowed=True)
        category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        session.add_all([user, category])
        await session.flush()
        alert = Alert(user_id=user.id, category_id=category.id, name="Ancienne alerte",
                      isbn="9782756079332", include_keywords=["ancienne"])
        missing = Alert(user_id=user.id, category_id=category.id, name="Sans livre",
                        include_keywords=["sans"])
        book = Book(title="Mauvais ISBN", normalized_title="mauvais isbn", isbn13="9781799510871")
        session.add_all([alert, missing, book])
        await session.flush()
        session.add(AlertBook(alert_id=alert.id, book_id=book.id))
        cache = BookMarketCache(book_id=book.id, checked_at=datetime.now(UTC),
                                historical_low_1y_cents=100, collection_status="failed")
        session.add(cache)
        await session.commit()
        manifest = await inventory_market_backfill(session)
        assert len(manifest["alerts"]) == 2
        assert manifest["books"][0]["offers_succeeded_at"] is None
        assert manifest["books"][0]["history_succeeded_at"] is None
        assert not manifest["books"][0]["source_history_verified"]
        assert {row["diagnostic"] for row in manifest["alerts"]} == {
            "identity_mismatch", "catalogue_missing",
        }
        assert manifest_digest(manifest) == manifest["digest"]
        assert not session.dirty
        await session.rollback()
        async with database.maker() as reader:
            stored = (await reader.scalars(select(BookMarketCache))).one()
            assert stored.last_successful_collection_at is None
            assert stored.historical_checked_at is None
            assert stored.source_history is None
            assert stored.next_refresh_at is None


async def test_backfill_rejects_a_changed_manifest_without_any_collection(monkeypatch):
    from unittest.mock import AsyncMock

    import pytest

    from app.services import book_market_pricing
    from app.services.market_backfill import apply_market_backfill, manifest_digest

    manifest = {"alerts": [], "books": [], "version": 1}
    digest = manifest_digest(manifest)
    manifest["digest"] = digest
    manifest["books"].append({"book_id": "unexpected"})
    collector = AsyncMock()
    monkeypatch.setattr(book_market_pricing, "price_book_now", collector)
    with pytest.raises(ValueError, match="Inventaire"):
        await apply_market_backfill(manifest, digest=digest)
    collector.assert_not_awaited()


async def test_isbn_alias_relink_is_idempotent_and_preserves_both_caches():
    from app.db.models.alert import Alert
    from app.db.models.alert_book import AlertBook
    from app.db.models.category import Category
    from app.db.models.user import User
    from app.db.repositories.book_repo import BookRepository

    async with postgres_replayed_database() as database:
        session = database.session
        user = User(telegram_user_id=8383, is_allowed=True)
        category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        session.add_all([user, category])
        await session.flush()
        alert = Alert(user_id=user.id, category_id=category.id, name="Blacksad",
                      include_keywords=["blacksad"])
        alias = Book(title="Blacksad", normalized_title="blacksad", isbn13="2205059254")
        canonical = Book(title="Blacksad", normalized_title="blacksad", isbn13="9782205059250")
        session.add_all([alert, alias, canonical])
        await session.flush()
        session.add_all([AlertBook(alert_id=alert.id, book_id=alias.id),
                         AlertBook(alert_id=alert.id, book_id=canonical.id),
                         BookMarketCache(book_id=alias.id, historical_low_1y_cents=1200),
                         BookMarketCache(book_id=canonical.id, historical_low_1y_cents=1500)])
        await session.commit()
        repo = BookRepository(session)
        options = {"isbn13": canonical.isbn13, "alert_ids": {alert.id}}
        assert await repo.relink_isbn_alias(alias.id, canonical.id, **options) == [alert.id]
        await session.commit()
        assert await repo.relink_isbn_alias(alias.id, canonical.id, **options) == []
        await session.commit()
        links = list((await session.scalars(select(AlertBook))).all())
        assert [link.book_id for link in links] == [canonical.id]
        assert len(list((await session.scalars(select(Book))).all())) == 2
        caches = list((await session.scalars(select(BookMarketCache))).all())
        assert sorted(cache.historical_low_1y_cents for cache in caches) == [1200, 1500]
        assert all(cache.last_successful_collection_at is None for cache in caches)


async def test_approved_catalogue_repair_keeps_old_data_and_alert_pause():
    from app.core.constants import AlertStatus
    from app.db.models.alert import Alert
    from app.db.models.alert_book import AlertBook
    from app.db.models.category import Category
    from app.db.models.user import User
    from app.services.market_backfill import apply_catalogue_repair

    async with postgres_replayed_database() as database:
        session = database.session
        user = User(telegram_user_id=8484, is_allowed=True)
        category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        session.add_all([user, category])
        await session.flush()
        alert = Alert(user_id=user.id, category_id=category.id, name="Nanbanjin",
                      isbn="9782808500487", status=AlertStatus.PAUSED,
                      include_keywords=["nanbanjin"])
        old = Book(title="Nanbanjin", normalized_title="nanbanjin", isbn13="9782808515023")
        session.add_all([alert, old])
        await session.flush()
        session.add_all([AlertBook(alert_id=alert.id, book_id=old.id),
                         BookMarketCache(book_id=old.id, historical_low_1y_cents=1200)])
        await session.commit()
        repair = {"alert_id": str(alert.id), "expected_name": alert.name,
                  "expected_isbn": alert.isbn, "expected_book_ids": [str(old.id)],
                  "isbn13": "9782808500487", "title": "Nanbanjin", "language": "fr",
                  "evidence_url": "https://www.dupuis.com/nanbanjin"}
        new_id = await apply_catalogue_repair(session, repair)
        assert new_id is not None
        assert new_id != old.id
        assert await apply_catalogue_repair(session, repair) == new_id
        books = list((await session.scalars(select(Book))).all())
        assert len(books) == 2
        assert (await session.scalars(select(AlertBook.book_id))).one() == new_id
        await session.refresh(alert)
        assert alert.status == AlertStatus.PAUSED
        old_cache = (await session.scalars(select(BookMarketCache))).one()
        assert old_cache.book_id == old.id
        assert old_cache.historical_low_1y_cents == 1200
        assert old_cache.last_successful_collection_at is None
