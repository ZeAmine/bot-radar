import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BookMarketCache
from app.services import book_market_pricing as pricing
from app.services.market_refresh_policy import reservation_lock


@pytest.mark.asyncio
async def test_listener_registration_failure_does_not_acquire_the_reservation_lock(monkeypatch):
    def fail_registration(*_args, **_kwargs):
        raise RuntimeError("listener_registration_failed")

    monkeypatch.setattr(pricing.event, "listen", fail_registration)
    async with AsyncSession() as session:
        cache = BookMarketCache(book_id=uuid.uuid4(), collection_status="complete")
        lock = reservation_lock()
        assert not lock.locked()
        with pytest.raises(RuntimeError, match="listener_registration_failed"):
            await pricing._reserve_next_refresh(session, cache.book_id, cache)
        assert not lock.locked()
        assert "price_reservation_lock" not in session.info
