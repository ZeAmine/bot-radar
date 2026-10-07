"""Un double clic confirmé ne doit pas compter deux faux positifs."""

from sqlalchemy import func, select

from app.db.models.bot_event import BotEvent
from app.db.models.listing import Listing
from app.db.repositories.autotune_repo import record_listing_dislike
from tests.database import postgres_replayed_database


async def test_confirming_twice_records_one_dislike():
    async with postgres_replayed_database() as database:
        session = database.session
        listing = Listing(
            source_name="vinted",
            external_id="feedback",
            title="Livre",
            normalized_title="livre",
            url="https://vinted.fr/items/1",
            category_slug="bd_comics",
            item_price_cents=1000,
            photos=[],
            raw_payload={},
        )
        session.add(listing)
        await session.commit()
        for _ in range(2):
            await record_listing_dislike(session, listing_id=listing.id, alert_id=None, chat_id=42)
            await session.commit()
        assert await session.scalar(select(func.count()).select_from(BotEvent)) == 1
