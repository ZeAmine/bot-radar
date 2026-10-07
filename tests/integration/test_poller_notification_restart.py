"""Une notification en attente survit à la reconstruction des annonces déjà vues."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram import Bot
from sqlalchemy import event, insert, select

from app.core.config import settings
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.db.repositories.listing_repo import ListingRepository
from app.domain.enums import AlertStatus
from app.schemas.listing import ListingIngestPayload, ListingIngestResponse
from app.services.vinted_scraper import ScrapedVintedItem, to_ingest_payload
from app.tasks.vinted_poller import FifoSet, load_initial_seen_ids, poll_vinted_once
from app.use_cases.ingest_listing import ProcessListingUseCase
from tests.database import FreshDatabase, postgres_replayed_database


@dataclass
class RestartJourney:
    """Joue le vrai cas d'usage et un catalogue doublé, avec des sessions distinctes."""

    database: FreshDatabase
    category: Category
    item: ScrapedVintedItem
    bot: AsyncMock

    async def add_alert(
        self, telegram_id: int = 601, *, is_allowed: bool = True, keyword: str = "console"
    ) -> tuple[Alert, User]:
        async with self.database.maker() as session:
            user = User(telegram_user_id=telegram_id, is_allowed=is_allowed)
            session.add(user)
            await session.flush()
            alert = Alert(
                user_id=user.id,
                category_id=self.category.id,
                name="Console",
                include_keywords=[keyword],
                exclude_keywords=[],
                variants=[],
                max_item_price_cents=5000,
                status=AlertStatus.ACTIVE,
            )
            session.add(alert)
            await session.commit()
            return alert, user

    async def ingest(self) -> ListingIngestResponse:
        raw = to_ingest_payload(self.item, category_slug="tech")
        payload = ListingIngestPayload.model_validate(raw)
        async with self.database.maker() as session:
            return await ProcessListingUseCase(session, bot=self.bot).execute(payload, raw)

    async def matches(self) -> list[ListingMatch]:
        async with self.database.maker() as session:
            return list((await session.execute(select(ListingMatch))).scalars().all())

    async def collect(self, seen: FifoSet) -> int:
        return await poll_vinted_once(
            seen,
            vinted_client=AsyncMock(spec=httpx.AsyncClient),
            category_slug="tech",
            cycle_index=1,
        )


@pytest.fixture
async def journey(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RestartJourney]:
    async with postgres_replayed_database() as database:
        # Les sessions du parcours gardent aussi l'autoflush de production.
        database.maker.configure(autoflush=False)
        category = Category(slug="tech", label="Tech", emoji="T", is_active=True)
        database.session.add(category)
        await database.session.commit()
        item = ScrapedVintedItem(
            external_id="restart-console",
            title="Console de jeu",
            condition="Très bon état",
            item_price_cents=1000,
            url="https://www.vinted.fr/items/restart-console",
            photo_url=None,
        )
        bot = AsyncMock(spec=Bot)
        bot.send_message.return_value = SimpleNamespace(message_id=42)
        monkeypatch.setattr(settings, "AI_ENABLED", False)
        monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [])
        monkeypatch.setattr("app.tasks.vinted_poller.AsyncSessionLocal", database.maker)
        monkeypatch.setattr("app.use_cases.ingest_listing.default_bot", bot)
        monkeypatch.setattr("app.tasks.vinted_poller._ingest_semaphore", None)
        monkeypatch.setattr(
            "app.tasks.vinted_poller.fetch_vinted_page", AsyncMock(return_value=[item])
        )
        yield RestartJourney(database, category, item, bot)


@pytest.mark.parametrize(("telegram_id", "is_allowed"), [(601, True), (-601, True), (601, False)])
async def test_failed_notification_is_retried_after_seen_reconstruction(
    journey: RestartJourney, telegram_id: int, is_allowed: bool
) -> None:
    """Un vrai match persisté en attente revient au prochain catalogue après redémarrage."""
    await journey.add_alert(telegram_id, is_allowed=is_allowed)
    journey.bot.send_message.side_effect = RuntimeError("Envoi Telegram factice indisponible")
    first = await journey.ingest()
    assert first.needs_retry is True
    assert first.matches[0].notification_sent is False
    matches = await journey.matches()
    assert len(matches) == 1
    assert matches[0].notified_at is None

    seen = await load_initial_seen_ids()
    assert journey.item.external_id not in seen

    journey.bot.send_message.reset_mock()
    journey.bot.send_message.side_effect = None
    assert await journey.collect(seen) == 1
    assert [call.kwargs["chat_id"] for call in journey.bot.send_message.call_args_list] == [
        telegram_id
    ]
    matches = await journey.matches()
    assert len(matches) == 1
    assert matches[0].notified_at is not None
    assert journey.item.external_id in await load_initial_seen_ids()


async def test_partial_success_retries_only_the_pending_recipient(journey: RestartJourney) -> None:
    sent_alert, _ = await journey.add_alert(601)
    pending_alert, _ = await journey.add_alert(602)

    async def send(chat_id: int, **_kwargs: object) -> SimpleNamespace:
        if chat_id == 602:
            raise RuntimeError("Envoi Telegram factice indisponible")
        return SimpleNamespace(message_id=42)

    journey.bot.send_message.side_effect = send
    assert (await journey.ingest()).needs_retry is True
    states = {match.alert_id: match.notified_at for match in await journey.matches()}
    assert states[sent_alert.id] is not None
    assert states[pending_alert.id] is None
    seen = await load_initial_seen_ids()
    assert journey.item.external_id not in seen

    journey.bot.send_message.reset_mock()
    journey.bot.send_message.side_effect = None
    assert await journey.collect(seen) == 1
    assert [call.kwargs["chat_id"] for call in journey.bot.send_message.call_args_list] == [602]
    matches = await journey.matches()
    assert len(matches) == 2
    assert all(match.notified_at is not None for match in matches)


async def test_fully_notified_listing_stays_seen_and_is_not_sent_again(
    journey: RestartJourney,
) -> None:
    await journey.add_alert(601)
    await journey.add_alert(602)
    assert (await journey.ingest()).needs_retry is False
    matches = await journey.matches()
    assert len(matches) == 2
    assert all(match.notified_at is not None for match in matches)
    seen = await load_initial_seen_ids()
    assert journey.item.external_id in seen
    journey.bot.send_message.reset_mock()
    assert await journey.collect(seen) == 0
    assert (await journey.ingest()).matches == []
    journey.bot.send_message.assert_not_awaited()


async def test_listing_without_a_match_keeps_the_initial_seen_behavior(
    journey: RestartJourney,
) -> None:
    await journey.add_alert(keyword="ordinateur")
    assert (await journey.ingest()).matches == []
    assert await journey.matches() == []
    seen = await load_initial_seen_ids()
    assert journey.item.external_id in seen
    assert await journey.collect(seen) == 0
    journey.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("status", [AlertStatus.PAUSED, AlertStatus.DELETED])
async def test_pending_inactive_recipient_does_not_make_the_listing_unseen(
    journey: RestartJourney, status: AlertStatus
) -> None:
    alert, _ = await journey.add_alert()
    journey.bot.send_message.side_effect = RuntimeError("Envoi Telegram factice indisponible")
    assert (await journey.ingest()).needs_retry is True
    async with journey.database.maker() as session:
        loaded = await session.get(Alert, alert.id)
        assert loaded is not None
        repo = AlertRepository(session)
        if status == AlertStatus.DELETED:
            await repo.soft_delete(loaded)
        else:
            await repo.set_status(loaded, status)
        await session.commit()

    seen = await load_initial_seen_ids()
    assert journey.item.external_id in seen
    journey.bot.send_message.reset_mock()
    assert await journey.collect(seen) == 0
    assert (await journey.ingest()).matches == []
    journey.bot.send_message.assert_not_awaited()
    assert (await journey.matches())[0].notified_at is None


async def test_pending_user_without_a_telegram_chat_stays_seen(journey: RestartJourney) -> None:
    _, user = await journey.add_alert()
    journey.bot.send_message.side_effect = RuntimeError("Envoi Telegram factice indisponible")
    assert (await journey.ingest()).needs_retry is True
    async with journey.database.maker() as session:
        loaded = await session.get(User, user.id)
        assert loaded is not None
        loaded.telegram_user_id = 0
        await session.commit()

    seen = await load_initial_seen_ids()
    assert journey.item.external_id in seen
    journey.bot.send_message.reset_mock()
    assert await journey.collect(seen) == 0
    assert (await journey.ingest()).needs_retry is False
    journey.bot.send_message.assert_not_awaited()
    assert (await journey.matches())[0].notified_at is None


async def test_seen_reconstruction_reads_all_notification_states_in_one_query(
    journey: RestartJourney,
) -> None:
    await journey.add_alert(601)
    await journey.add_alert(602)
    journey.bot.send_message.side_effect = RuntimeError("Envoi Telegram factice indisponible")
    for external_id in ("first-pending", "second-pending"):
        journey.item.external_id = external_id
        await journey.ingest()
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    engine = journey.database.session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        seen = await load_initial_seen_ids()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert seen == set()


async def test_seen_reconstruction_preserves_source_filter_and_fifo_order(
    journey: RestartJourney,
) -> None:
    for external_id in ("oldest", "newest"):
        journey.item.external_id = external_id
        await journey.ingest()
    raw = to_ingest_payload(journey.item, category_slug="tech")
    raw.update(source_name="other", external_id="foreign")
    async with journey.database.maker() as session:
        await ListingRepository(session).create_from_payload(
            ListingIngestPayload.model_validate(raw), raw
        )
        await session.commit()

    seen = await load_initial_seen_ids()
    assert seen == {"oldest", "newest"}
    seen.max_size = 2
    seen.trim_size = 2
    seen.add("next")
    assert seen == {"newest", "next"}


async def test_pending_exclusion_does_not_expand_the_recent_listing_window(
    journey: RestartJourney,
) -> None:
    """Retirer une attente ne masque pas une annonce au-delà de la fenêtre initiale."""
    await journey.add_alert()
    journey.bot.send_message.side_effect = RuntimeError("Envoi Telegram factice indisponible")
    assert (await journey.ingest()).needs_retry is True
    async with journey.database.maker() as session:
        pending = (await session.execute(select(Listing))).scalar_one()
        await session.execute(
            insert(Listing),
            [
                {
                    "source_name": "vinted",
                    "external_id": f"old-{index}",
                    "title": "Console",
                    "normalized_title": "console",
                    "url": "https://www.vinted.fr/items/old-console",
                    "category_slug": "tech",
                    "item_price_cents": 1000,
                    "photos": [],
                    "raw_payload": {},
                    "received_at": pending.received_at - timedelta(seconds=index + 1),
                }
                for index in range(3000)
            ],
        )
        await session.commit()

    seen = await load_initial_seen_ids()
    assert len(seen) == 2999
    assert journey.item.external_id not in seen
    assert "old-2998" in seen
    assert "old-2999" not in seen
