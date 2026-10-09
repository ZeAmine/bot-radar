"""Une seule validité de lot, avec horloge locale et aucune collecte."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.domain.enums import ComparisonStatus
from app.services import market_price_service as market
from app.services.tome_extractor import TomeMatchResult

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
pytestmark = pytest.mark.usefixtures("clock")


@pytest.fixture
def clock(monkeypatch):
    moment = SimpleNamespace(now=NOW)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.now if tz else moment.now.replace(tzinfo=None)

    monkeypatch.setattr(market, "datetime", Clock)
    return moment


def book(n, price=1000, *, expires=NOW + timedelta(hours=24), historical=None):
    value = Book(id=uuid.uuid4(), title=f"Cible BD Tome {n}")
    value.market_cache = BookMarketCache(
        lowest_delivered_price_cents=price,
        historical_low_1y_cents=historical,
        expires_at=expires,
        checked_at=NOW,
        last_attempt_at=NOW,
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    return value


def render(books, wanted):
    listing = SimpleNamespace(title="Lot Cible BD", item_price_cents=100, estimated_total_cents=500)
    tome = TomeMatchResult(is_lot=True, lot_tomes=wanted, confidence="high")
    return (
        market.MarketPriceService.get_comparison_summary(listing, None, books, tome),
        market.MarketPriceService.format_market_details_card(listing, None, books, tome),
    )


def test_complete_reference_and_announced_subset():
    books = [book(1, 1000), book(2, 2000), book(3, 3000)]
    summary, panel = render(books, [1, 2])
    assert "30,00" in summary
    assert "Écart" in summary
    assert "Total du lot :</b> ~30,00" in panel
    assert "60,00" not in summary


@pytest.mark.parametrize(
    "reason", ["expired", "undated", "missing", "missing_book", "duplicate", "none"]
)
def test_unreliable_lot_has_no_complete_total_or_global_gap(reason):
    books = [book(1), book(2, 2000)]
    if reason == "expired":
        books[1].market_cache.expires_at = NOW - timedelta(seconds=1)
    elif reason == "undated":
        books[1].market_cache.expires_at = None
    elif reason == "missing":
        books[1].market_cache = None
    elif reason == "missing_book":
        books = books[:1]
    elif reason == "duplicate":
        books.append(book(1, 1500))
    else:
        books = []
    summary, panel = render(books, [1, 2])
    assert "Écart" not in (summary or "")
    assert "Total du lot" not in panel
    assert "indisponible" in (summary or "").lower()
    if reason == "duplicate":
        assert "ambigu" in summary.lower()


def test_no_applicable_tome_never_falls_back_to_whole_series():
    summary, panel = render([book(1), book(2)], [8, 9])
    assert "Écart" not in summary
    assert "30,00" not in summary
    assert "Total du lot" not in panel
    assert "T1" not in panel


def test_expiration_without_new_write_invalidates_reference(clock):
    books = [book(1), book(2)]
    assert "Écart" in render(books, [1, 2])[0]
    clock.now += timedelta(hours=25)
    summary, panel = render(books, [1, 2])
    assert "Écart" not in summary
    assert "Total du lot" not in panel


@pytest.mark.parametrize("expires", [None, NOW - timedelta(days=2)])
def test_current_single_tome_uses_same_admissibility(expires):
    single = book(1, expires=expires)
    listing = SimpleNamespace(item_price_cents=100, estimated_total_cents=500)
    assert market.MarketPriceService.get_comparison_summary(listing, single) is None
    assert not market.has_usable_price(single.market_cache)


def test_historical_policy_is_preserved_without_annual_revalidation():
    books = [
        book(1, expires=None, historical=800),
        book(2, expires=NOW - timedelta(days=400), historical=900),
    ]
    summary, panel = render(books, [1, 2])
    assert "17,00" in summary
    assert "Prix bas constaté" in summary
    assert "Total du lot :</b> ~17,00" in panel


def test_incoherent_persisted_series_total_is_only_hidden():
    books = [book(1), book(2, 2000)]
    text = market.MarketPriceService.format_market_details_card(
        None, None, books, series_total_cents=6000
    )
    assert "Total de la série" not in text
    # La lecture ne publie pas une somme de remplacement.
    assert "~30,00" not in text


def test_series_total_expires_at_read_without_any_write(clock):
    books = [book(1), book(2, 2000)]
    def panel():
        return market.MarketPriceService.format_market_details_card(
            None, None, books, series_total_cents=3000
        )
    assert "Total de la série :</b> ~30,00" in panel()
    clock.now += timedelta(hours=25)
    assert "Total de la série" not in panel()


def test_selected_tome_does_not_replace_the_announced_lot_reference():
    books = [book(1), book(2, 2000)]
    listing = SimpleNamespace(item_price_cents=100, estimated_total_cents=500)
    tome = TomeMatchResult(is_lot=True, lot_tomes=[1, 2, 3], confidence="high")
    summary = market.MarketPriceService.get_comparison_summary(listing, books[0], books, tome)
    panel = market.MarketPriceService.format_market_details_card(
        listing, books[0], books, tome, series_total_cents=3000
    )
    assert "Écart" not in summary
    assert "Total de la série" not in panel
    assert "Total du lot" not in panel
    assert "incomplète" in panel
    assert "10,00" in panel


def test_selected_tome_preserves_complete_lot_total():
    books = [book(1), book(2, 2000)]
    tome = TomeMatchResult(is_lot=True, lot_tomes=[1, 2], confidence="high")
    panel = market.MarketPriceService.format_market_details_card(
        None, books[0], books, tome, series_total_cents=3000
    )
    assert "Total du lot :</b> ~30,00" in panel
    assert "Total de la série" not in panel


def test_unavailable_large_lot_keeps_telegram_summary_compact():
    books = [book(n, expires=None) for n in range(1, 100)]
    summary, panel = render(books, list(range(1, 100)))
    assert len(summary) < 512
    assert "détails" in summary
    assert "Tome 99" in panel


def test_attempt_without_observation_does_not_date_a_price():
    single = book(1)
    single.market_cache.checked_at = None
    text = market.MarketPriceService.format_market_details_card(None, single)
    assert "Actualisation :</b> Non renseignée" in text
