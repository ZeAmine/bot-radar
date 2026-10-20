"""Persistance et relecture du prix confirmé et des composantes indépendantes."""

from datetime import UTC, datetime, timedelta

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.providers.base import CollectionAttempt, MarketOffer, MarketOffers
from app.services.book_market_pricing import BookPricingResult, persist_market_result, pricing_stat
from app.services.market_price_service import (
    _current_collection_is_empty,
    admissible_reference_price,
)
from app.services.market_refresh_policy import component_due
from tests.database import postgres_replayed_database


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _offer(provider: str, cents: int, *, observed_at: datetime,
           historical: bool = False) -> MarketOffer:
    return MarketOffer(
        provider_name=provider,
        item_price_cents=cents,
        shipping_price_cents=None,
        delivered_price_cents=cents,
        source_url=f"https://{provider}.example/{cents}",
        item_condition="Très bon état +" if historical else "Très bon état",
        price_includes_shipping=True,
        is_historical=historical,
        is_available=not historical,
        confidence=MatchConfidence.HIGH,
        observed_at=observed_at,
        raw_payload={"best_merchant": provider},
    )


async def test_confirmed_price_is_read_back_not_the_retained_partial_cal_offer() -> None:
    async with postgres_replayed_database() as database:
        session = database.session
        now = datetime.now(UTC)
        old = now - timedelta(days=8)
        old_expires = now + timedelta(hours=2)
        book = Book(title="Livre", normalized_title="livre")
        session.add(book)
        await session.flush()
        old_snapshot = MarketPriceSnapshot(
            book_id=book.id,
            provider_name="chasse_aux_livres",
            item_price_cents=800,
            delivered_price_cents=800,
            source_url="https://chasse.example/800",
            item_condition="Très bon état",
            checked_at=old,
            observed_at=old,
            is_historical=False,
        )
        retained = {
            "provider_name": "chasse_aux_livres", "delivered_price_cents": 800,
            "source_url": "https://chasse.example/800", "item_condition": "Très bon état",
        }
        cache = BookMarketCache(
            book_id=book.id,
            lowest_delivered_price_cents=800,
            best_provider_name="chasse_aux_livres",
            best_source_url="https://chasse.example/800",
            comparison_status=ComparisonStatus.AVAILABLE,
            checked_at=old,
            expires_at=old_expires,
            last_successful_collection_at=old,
            last_attempt_at=old,
            current_offers=[retained],
        )
        session.add_all([old_snapshot, cache])
        await session.commit()
        cache_id = cache.id
        snapshot_id = old_snapshot.id

        cal = _offer("chasse_aux_livres", 1500, observed_at=now)
        healthy = _offer("momox", 1000, observed_at=now)
        result = BookPricingResult.from_offers(
            MarketOffers(
                [cal, healthy],
                failed_providers=("chasse_aux_livres",),
                attempts=(
                    CollectionAttempt(
                        "chasse_aux_livres", "current", "partial", "incomplete_offers",
                    ),
                    CollectionAttempt("momox", "current"),
                ),
            ),
            healthy,
            None,
        )
        assert result.price_confirmed is True

        await persist_market_result(session, book.id, result)
        await session.commit()

        async with database.maker() as reader:
            stored = await reader.get(BookMarketCache, cache_id)
            assert stored is not None
            assert stored.lowest_delivered_price_cents == 1000
            assert stored.best_provider_name == "momox"
            assert [offer["provider_name"] for offer in stored.current_offers] == ["momox"]
            assert admissible_reference_price(stored)[0] == 1000
            assert pricing_stat(stored) == "available"
            assert _current_collection_is_empty(stored) is False
            old_observation = await reader.get(MarketPriceSnapshot, snapshot_id)
            assert old_observation is not None
            assert old_observation.delivered_price_cents == 800


async def test_history_only_does_not_touch_the_current_cache_or_its_due_date() -> None:
    async with postgres_replayed_database() as database:
        session = database.session
        now = datetime.now(UTC)
        old = now - timedelta(days=2)
        due = old + timedelta(days=7)
        old_expires = now + timedelta(hours=2)
        current_offers = [
            {
                "provider_name": "chasse_aux_livres", "item_price_cents": 800,
                "shipping_price_cents": None, "delivered_price_cents": 800,
                "source_url": "https://chasse.example/800",
                "item_condition": "Très bon état", "price_includes_shipping": True,
            },
            {
                "provider_name": "momox", "delivered_price_cents": 1000,
                "source_url": "https://momox.example/1000", "item_condition": "Très bon état",
            },
        ]
        cache = BookMarketCache(
            book_id=None,
            lowest_delivered_price_cents=1000,
            best_provider_name="momox",
            best_source_url="https://momox.example/1000",
            comparison_status=ComparisonStatus.AVAILABLE,
            checked_at=old,
            expires_at=old_expires,
            last_successful_collection_at=old,
            last_attempt_at=old,
            collection_status="complete",
            current_offers=current_offers,
            failed_provider_names=["ancienne-source"],
            refresh_state={"current": {"due_at": due.isoformat(), "failures": 0}},
        )
        book = Book(title="Livre", normalized_title="livre", market_cache=cache)
        session.add(book)
        await session.flush()
        cache.book_id = book.id
        await session.commit()
        cache_id = cache.id

        point = _offer(
            "chasse_aux_livres", 900, observed_at=now - timedelta(days=1), historical=True,
        )
        result = BookPricingResult.from_offers(
            MarketOffers(
                [point],
                historical_checked=True,
                attempts=(
                    CollectionAttempt("chasse_aux_livres", "current", "skipped"),
                    CollectionAttempt("chasse_aux_livres", "history"),
                ),
            ),
            None,
            900,
        )
        await persist_market_result(session, book.id, result)
        await session.commit()

        async with database.maker() as reader:
            stored = await reader.get(BookMarketCache, cache_id)
            assert stored is not None
            assert stored.lowest_delivered_price_cents == 1000
            assert stored.current_offers == current_offers
            assert stored.collection_status == "complete"
            assert _as_utc(stored.last_successful_collection_at) == old
            assert _as_utc(stored.last_attempt_at) == old
            assert _as_utc(stored.checked_at) == old
            assert _as_utc(stored.expires_at) == old_expires
            assert stored.failed_provider_names == ["ancienne-source"]
            assert stored.refresh_state["current"] == {
                "due_at": due.isoformat(), "failures": 0,
            }
            assert _as_utc(stored.historical_checked_at) == result.collected_at
            assert stored.historical_low_1y_cents == 900
            assert stored.source_history is not None
            assert stored.source_history["price_cents"] == 900
            assert pricing_stat(stored) == "available"
            assert _current_collection_is_empty(stored) is False
            assert component_due(stored, "current", now) == due
