from datetime import datetime

from app.db.models.book_market_cache import BookMarketCache


def source_history_values(price: int | None, moment: datetime) -> dict:
    if price is None:
        return {}
    return {
        "source_history": {"price_cents": price, "condition": "Très bon état +", "days": 365,
                           "shipping_included": True, "collected_at": moment.isoformat()},
        "historical_checked_at": moment,
    }


def attach_source_history(cache: BookMarketCache, price: int | None, moment: datetime) -> None:
    for key, value in source_history_values(price, moment).items():
        setattr(cache, key, value)
