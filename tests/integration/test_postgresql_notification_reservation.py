"""Réservations conditionnelles réelles, sans transport et sur base synthétique."""

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.db.models.user import User
from app.schemas.listing import ListingIngestPayload
from app.use_cases import notification_admission as admission
from tests.database import postgres_replayed_database


@pytest.mark.skipif(
    make_url(os.environ.get("TEST_DATABASE_URL", "sqlite://")).get_backend_name() != "postgresql",
    reason="Réservations concurrentes : transactions PostgreSQL indépendantes requises",
)
async def test_conditional_reservation_and_independent_pair(monkeypatch, record_property):
    async with postgres_replayed_database() as db:
        assert db.session.get_bind().dialect.name == "postgresql"
        user = User(telegram_user_id=424242, is_allowed=True)
        category = Category(slug="tech", label="Tech", emoji="T", is_active=True)
        db.session.add_all([user, category])
        await db.session.flush()
        alert = Alert(user_id=user.id, category_id=category.id, name="Console",
                      include_keywords=["console"], status="ACTIVE")
        db.session.add(alert)
        await db.session.flush()
        from app.db.repositories.listing_repo import ListingRepository

        listings = []
        for external_id in ("same", "independent"):
            listing = await ListingRepository(db.session).create_from_payload(
                ListingIngestPayload(source_name="synthetic", external_id=external_id,
                    title="Console", url="https://example.invalid/item", category_slug="tech",
                    item_price_cents=100), {},
            )
            row = ListingMatch(alert_id=alert.id, listing_id=listing.id,
                               notification_state="ready", attempt_count=0)
            db.session.add(row)
            listings.append(listing.id)
        await db.session.commit()
        monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [424242])
        arrived, blocked, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_read = admission.notification_row
        readers = 0
        pids = []

        async def read(session, alert_id, listing_id):
            nonlocal readers
            row = await original_read(session, alert_id, listing_id)
            if listing_id == listings[0]:
                readers += 1
                if readers == 2:
                    arrived.set()
                await asyncio.wait_for(arrived.wait(), 3)
                if not session.info["hold"]:
                    await asyncio.wait_for(blocked.wait(), 3)
            return row

        monkeypatch.setattr(admission, "notification_row", read)

        async def reserve(listing_id, hold):
            async with db.maker() as session:
                session.info["hold"] = hold
                pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
                current = await session.scalar(select(Alert).where(Alert.id == alert.id)
                                               .options(selectinload(Alert.user)))
                listing = await session.get(Listing, listing_id)
                original_commit = session.commit

                async def commit():
                    if hold:
                        blocked.set()
                        await asyncio.wait_for(release.wait(), 3)
                    await original_commit()

                monkeypatch.setattr(session, "commit", commit)
                return await admission.reserve(
                    session, listing, current, SimpleNamespace(id=123456789), uuid.uuid4(),
                )

        first = asyncio.create_task(reserve(listings[0], True))
        # Les deux lisent READY ; le second attend l'UPDATE non commité du premier.
        second = asyncio.create_task(reserve(listings[0], False))
        participants = [first, second]
        try:
            await asyncio.wait_for(blocked.wait(), 3)
            assert not first.done()
            assert not second.done()
            independent = asyncio.create_task(reserve(listings[1], False))
            participants.append(independent)
            assert await asyncio.wait_for(independent, 3)
            release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), 3)
            assert sorted(results) == [False, True]
            assert len(set(pids)) == 3
            record_property("postgres_recipe", {"participants": pids, "reservations": results})
        finally:
            arrived.set()
            release.set()
            for participant in participants:
                if not participant.done():
                    participant.cancel()
            await asyncio.wait_for(asyncio.gather(*participants, return_exceptions=True), 3)
        async with db.maker() as reader:
            rows = list((await reader.scalars(select(ListingMatch))).all())
            assert len(rows) == 2
            assert all(row.notification_state == "sending" for row in rows)
            assert all(row.attempt_count == 1 for row in rows)
            assert all(row.notified_at is None and row.telegram_message_id is None for row in rows)
