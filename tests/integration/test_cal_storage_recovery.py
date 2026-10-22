import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.providers.cal_governor import CalGovernor
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
from app.providers.registry import ProviderRegistry
from app.services.book_market_pricing import price_book
from app.services.market_health import CalStorageHealth
from tests.database import postgres_replayed_database

pytest_plugins = ["tests.unit.cal_redis_fixture"]


async def _redis_action(control: Path, action: str) -> None:
    ack = control / "ack"
    ack.unlink(missing_ok=True)
    temporary = control / "pending"
    temporary.write_text(action)
    temporary.replace(control / "request")
    async with asyncio.timeout(30):
        while not ack.exists():
            await asyncio.sleep(0.05)
    assert ack.read_text() == action


async def test_real_redis_outage_defers_persists_alerts_once_and_recovers(
    clean_cal_keys, cal_namespace,
):
    directory = os.environ.get("CAL_TEST_REDIS_CONTROL_DIR")
    if not directory:
        pytest.skip("Recette verify_cal requise pour arrêter le Redis jetable")
    control = Path(directory)
    now = [0.0]
    governor = CalGovernor(store=clean_cal_keys, namespace=cal_namespace,
                           monotonic_clock=lambda: now[0])
    messages, requests = [], []

    async def notify(message):
        messages.append(message)
        return "accepted"

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, text="")

    health = CalStorageHealth(governor=governor, notify=notify)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = ChasseAuxLivresProvider(http_client=client, governor=governor, max_retries=0)
        registry = ProviderRegistry([provider])
        async with postgres_replayed_database() as db:
            book = Book(title="Livre", normalized_title="livre")
            db.session.add(book)
            await db.session.flush()
            old = datetime.now(UTC) - timedelta(days=10)
            cache = BookMarketCache(
                book_id=book.id, last_attempt_at=old,
                last_successful_collection_at=old, historical_checked_at=old,
                refresh_state={"current": {"failures": 2, "due_at": old.isoformat()},
                               "history": {"failures": 1, "due_at": old.isoformat()}},
            )
            db.session.add(cache)
            await db.session.commit()
            await governor.snapshot()
            await _redis_action(control, "stop")
            try:
                await price_book(db.session, book, registry=registry)
                await db.session.commit()
                async with db.maker() as reader:
                    stored = await reader.get(BookMarketCache, cache.id)
                    assert stored.collection_status == "deferred"
                    assert stored.last_attempt_at.replace(tzinfo=UTC) == old
                    assert stored.last_successful_collection_at.replace(tzinfo=UTC) == old
                    assert stored.historical_checked_at.replace(tzinfo=UTC) == old
                    assert stored.refresh_state["current"]["failures"] == 2
                    assert stored.refresh_state["history"]["failures"] == 1
                    assert not stored.failed_provider_names
                    assert stored.next_refresh_at is not None
                assert requests == []
                await health.check()
                now[0] = 299
                await health.check()
                assert messages == []
                now[0] = 300
                await health.check()
                await health.check()
                assert len(messages) == 1
            finally:
                await _redis_action(control, "start")
            async with asyncio.timeout(10):
                while True:
                    await governor.snapshot()
                    if not governor.storage_unavailable:
                        break
                    await asyncio.sleep(0.05)
            assert await governor.admit_book("recovered") is None
            await provider.get_offers(isbn13=None, title="Livre")
            assert requests
            await health.check()
            assert len(messages) == 1
