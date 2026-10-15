"""Les ingestions d'une annonce partagent leur protection, avec de vraies sessions."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.api.dependencies import get_db
from app.api.routes.ingest import router
from app.core.config import settings
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.db.models.user import User
from app.db.repositories.listing_repo import ListingRepository
from app.domain.enums import AlertStatus
from app.schemas.listing import ListingIngestPayload
from app.use_cases.ingest_listing import ProcessListingUseCase
from app.use_cases.listing_ingestion_gate import ListingIngestionGate
from tests.database import FreshDatabase, postgres_replayed_database
from tests.telegram_fakes import telegram_message


@dataclass
class IngestionJourney:
    """Base isolée et seul effet externe remplacé : la livraison Telegram."""

    database: FreshDatabase
    bot: AsyncMock
    category: Category
    deliveries: list[tuple[int, str]]

    def payload(self, external_id: str = "console-1", source: str = "vinted"):
        return ListingIngestPayload(
            source_name=source,
            external_id=external_id,
            url=f"https://example.invalid/{external_id}",
            title="Console de jeu",
            category_slug="tech",
            item_price_cents=1000,
        )

    async def add_alert(self, chat_id: int = 601):
        async with self.database.maker() as session:
            user = User(telegram_user_id=chat_id, is_allowed=True)
            session.add(user)
            await session.flush()
            alert = Alert(
                user_id=user.id,
                category_id=self.category.id,
                name="Console",
                include_keywords=["console"],
                exclude_keywords=[],
                variants=[],
                max_item_price_cents=5000,
                status=AlertStatus.ACTIVE,
            )
            session.add(alert)
            await session.commit()
            return alert

    async def ingest(self, external_id: str = "console-1", source: str = "vinted"):
        payload = self.payload(external_id, source)
        async with self.database.maker() as session:
            return await ProcessListingUseCase(session, self.bot).execute(
                payload, payload.model_dump(mode="json")
            )

    async def matches(self):
        async with self.database.maker() as session:
            return list((await session.execute(select(ListingMatch))).scalars())


@pytest.fixture
async def journey(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[IngestionJourney]:
    async with postgres_replayed_database() as database:
        database.maker.configure(autoflush=False)
        category = Category(slug="tech", label="Tech", emoji="T", is_active=True)
        database.session.add(category)
        await database.session.commit()
        bot = AsyncMock(spec=Bot)
        bot.id = 123456789
        deliveries: list[tuple[int, str]] = []

        async def send(chat_id: int, **kwargs):
            deliveries.append((chat_id, kwargs["text"]))
            return telegram_message(chat_id, len(deliveries))

        bot.send_message.side_effect = send
        monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [601, 602])
        monkeypatch.setattr(settings, "AI_ENABLED", False)
        monkeypatch.setattr("app.use_cases.ingest_listing.default_bot", bot)
        monkeypatch.setattr(
            "app.use_cases.ingest_listing.listing_ingestion_gate", ListingIngestionGate()
        )
        yield IngestionJourney(database, bot, category, deliveries)


@pytest.mark.parametrize("existing_pending", [False, True])
async def test_same_listing_waits_before_reading_and_sends_once(journey, existing_pending):
    """Sans acquisition avant lecture, le second traitement décide sur un état périmé."""
    alert = await journey.add_alert()
    if existing_pending:
        # Attente dont le non-envoi est établi par le semis, pas par une erreur réseau.
        async with journey.database.maker() as session:
            repo = ListingRepository(session)
            value = journey.payload()
            listing = await repo.create_from_payload(value, value.model_dump(mode="json"))
            await repo.create_match(alert.id, listing.id)
            await session.commit()
    entered, release, started, read = (asyncio.Event() for _ in range(4))

    async def send(chat_id: int, **kwargs):
        journey.deliveries.append((chat_id, kwargs["text"]))
        entered.set()
        await release.wait()
        return telegram_message(chat_id)

    journey.bot.send_message.side_effect = send
    first = asyncio.create_task(journey.ingest())
    async with journey.database.maker() as session:
        use_case = ProcessListingUseCase(session, journey.bot)
        original_read = use_case.listing_repo.get_by_source_and_external_id

        async def observe_read(**kwargs):
            read.set()
            return await original_read(**kwargs)

        use_case.listing_repo.get_by_source_and_external_id = observe_read

        async def second_ingestion():
            started.set()
            payload = journey.payload()
            return await use_case.execute(payload, payload.model_dump(mode="json"))

        await asyncio.wait_for(entered.wait(), 3)
        second = asyncio.create_task(second_ingestion())
        try:
            await started.wait()
            assert not read.is_set(), "La seconde ingestion lit avant la fin du premier envoi"
        finally:
            release.set()
            results = await asyncio.gather(first, second)
    assert results[1].is_duplicate
    assert results[1].matches == []
    assert len(journey.deliveries) == 1
    async with journey.database.maker() as session:
        assert len((await session.execute(select(Listing))).scalars().all()) == 1
    matches = await journey.matches()
    assert len(matches) == 1
    assert matches[0].notified_at is not None


async def test_distinct_listings_progress_while_another_send_waits(journey):
    """Un verrou global empêcherait l'annonce distincte de partir."""
    await journey.add_alert()
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(chat_id: int, **kwargs):
        journey.deliveries.append((chat_id, kwargs["text"]))
        if len(journey.deliveries) == 1:
            entered.set()
            await release.wait()
        return telegram_message(chat_id)

    journey.bot.send_message.side_effect = send
    first = asyncio.create_task(journey.ingest())
    await asyncio.wait_for(entered.wait(), 3)
    try:
        async with asyncio.timeout(2):
            result = await journey.ingest("console-2")
        assert result.matches[0].notification_sent
        assert not first.done()
    finally:
        release.set()
        await first
    assert len(journey.deliveries) == 2


async def test_partial_success_is_reread_after_waiting(journey):
    """Le succès committé du premier destinataire ne doit pas être rejoué."""
    await journey.add_alert(601)
    await journey.add_alert(602)
    entered, release, started = (asyncio.Event() for _ in range(3))
    attempts = []

    async def send(chat_id: int, **kwargs):
        attempts.append(chat_id)
        # SQL ne garantit pas l'ordre des alertes : le second appel échoue,
        # quel que soit son destinataire, après un premier succès committé.
        if len(attempts) == 2:
            entered.set()
            await release.wait()
            raise RuntimeError("Échec factice du second destinataire")
        journey.deliveries.append((chat_id, kwargs["text"]))
        return telegram_message(chat_id, len(journey.deliveries))

    async def second_ingestion():
        started.set()
        return await journey.ingest()

    journey.bot.send_message.side_effect = send
    first = asyncio.create_task(journey.ingest())
    await asyncio.wait_for(entered.wait(), 3)
    second = asyncio.create_task(second_ingestion())
    await started.wait()
    release.set()
    results = await asyncio.gather(first, second)
    assert not results[0].needs_retry
    assert not results[1].needs_retry
    assert attempts in ([601, 602], [602, 601])
    assert len(journey.deliveries) == 1
    assert sum(match.notified_at is not None for match in await journey.matches()) == 1
    assert sum(match.notification_state == "unknown" for match in await journey.matches()) == 1


async def test_waiter_refreshes_preloaded_listing_and_user(journey):
    """Une session gardant des objets ORM ne doit pas envoyer le titre ou le chat périmé."""
    alert = await journey.add_alert()
    async with journey.database.maker() as seed:
        repo = ListingRepository(seed)
        payload = journey.payload()
        listing = await repo.create_from_payload(payload, payload.model_dump(mode="json"))
        await repo.create_match(alert.id, listing.id)
        await seed.commit()
    entered, release, started = (asyncio.Event() for _ in range(3))

    async def fail_after_wait(**_kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError("Échec factice")

    async with journey.database.maker() as session:
        repo = ListingRepository(session)
        listing = await repo.get_by_source_and_external_id("vinted", "console-1")
        alerts = await repo.get_active_alerts_by_category("tech")
        assert alerts[0].user.telegram_user_id == 601
        assert listing.title == "Console de jeu"
        await session.commit()  # Objets conservés, transaction de lecture terminée.
        journey.bot.send_message.side_effect = fail_after_wait
        first = asyncio.create_task(journey.ingest())
        await asyncio.wait_for(entered.wait(), 3)

        async def second_ingestion():
            started.set()
            payload = journey.payload()
            return await ProcessListingUseCase(session, journey.bot).execute(
                payload, payload.model_dump(mode="json")
            )

        second = asyncio.create_task(second_ingestion())
        await started.wait()
        async with journey.database.maker() as editor:
            await editor.execute(update(Listing).values(title="Console actualisée"))
            await editor.execute(
                update(User).where(User.id == alert.user_id).values(telegram_user_id=602)
            )
            await editor.commit()

        async def success(chat_id: int, **kwargs):
            journey.deliveries.append((chat_id, kwargs["text"]))
            return telegram_message(chat_id)

        journey.bot.send_message.side_effect = success
        release.set()
        await asyncio.gather(first, second)
    assert journey.deliveries == []
    assert listing.title == "Console actualisée"
    assert alerts[0].user.telegram_user_id == 602
    assert (await journey.matches())[0].notification_state == "unknown"


@pytest.mark.parametrize(
    "stage", ["before_commit", "during_send", "before_mark_commit", "after_commit"]
)
async def test_holder_cancellation_leaves_persisted_state_and_allows_retry(
    journey, monkeypatch, stage
):
    """Annuler un détenteur ne laisse ni verrou local ni transaction SQL bloquante."""
    from app.tasks.vinted_poller import load_initial_seen_ids
    from app.use_cases.ingest_listing import listing_ingestion_gate

    await journey.add_alert()
    entered, never = asyncio.Event(), asyncio.Event()
    async with journey.database.maker() as session:
        original_commit = session.commit
        commits = 0

        async def commit():
            nonlocal commits
            commits += 1
            if (stage == "before_commit" and commits == 1) or (
                stage == "before_mark_commit" and commits == 3
            ):
                entered.set()
                await never.wait()
            await original_commit()
            if stage == "after_commit" and commits == 3:
                entered.set()
                await never.wait()

        async def send(chat_id: int, **kwargs):
            if stage == "during_send":
                entered.set()
                await never.wait()
            journey.deliveries.append((chat_id, kwargs["text"]))
            return telegram_message(chat_id)

        monkeypatch.setattr(session, "commit", commit)
        journey.bot.send_message.side_effect = send
        payload = journey.payload()
        task = asyncio.create_task(ProcessListingUseCase(session, journey.bot).execute(payload, {}))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Même avant la fermeture de la session : le rollback est déjà fait.
        assert not session.in_transaction()
        assert not listing_ingestion_gate._entries
        assert listing_ingestion_gate._participants == 0

    matches = await journey.matches()
    if stage == "before_commit":
        assert matches == []
    else:
        assert len(matches) == 1
        assert (matches[0].notified_at is not None) == (stage == "after_commit")
    monkeypatch.setattr("app.tasks.vinted_poller.AsyncSessionLocal", journey.database.maker)
    assert ("console-1" in await load_initial_seen_ids()) == (stage != "before_commit")
    journey.bot.send_message.side_effect = None
    journey.bot.send_message.return_value = telegram_message(message_id=99)
    result = await journey.ingest()
    assert len(result.matches) == (1 if stage == "before_commit" else 0)
    assert ((await journey.matches())[0].notified_at is not None) == (
        stage in {"before_commit", "after_commit"}
    )


@pytest.mark.parametrize("trigger", ["error", "cancellation"])
async def test_cancellation_during_rollback_does_not_release_key_early(
    journey, monkeypatch, trigger
):
    """Annuler le nettoyage ne doit pas admettre un autre traitement avant sa fin SQL."""
    from app.use_cases.ingest_listing import listing_ingestion_gate

    await journey.add_alert()
    entered, rollback_started, release_rollback, second_started, read = (
        asyncio.Event() for _ in range(5)
    )
    rollback_completed = False
    async with journey.database.maker() as session:
        original_rollback = session.rollback

        async def rollback():
            nonlocal rollback_completed
            rollback_started.set()
            await release_rollback.wait()
            await original_rollback()
            rollback_completed = True

        async def fail_commit():
            raise RuntimeError("Échec factice avant persistance")

        async def blocked_send(**_kwargs):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(session, "rollback", rollback)
        if trigger == "error":
            monkeypatch.setattr(session, "commit", fail_commit)
        else:
            journey.bot.send_message.side_effect = blocked_send
        first = asyncio.create_task(
            ProcessListingUseCase(session, journey.bot).execute(journey.payload(), {})
        )
        if trigger == "cancellation":
            await asyncio.wait_for(entered.wait(), 3)
            first.cancel()
        await rollback_started.wait()
        first.cancel()  # Annulation pendant le rollback, pas pendant le traitement initial.
        journey.bot.send_message.side_effect = None
        journey.bot.send_message.return_value = telegram_message(message_id=99)
        async with journey.database.maker() as second_session:
            use_case = ProcessListingUseCase(second_session, journey.bot)
            original_read = use_case.listing_repo.get_by_source_and_external_id

            async def observe_read(**kwargs):
                read.set()
                return await original_read(**kwargs)

            monkeypatch.setattr(
                use_case.listing_repo, "get_by_source_and_external_id", observe_read
            )

            async def retry():
                second_started.set()
                return await use_case.execute(journey.payload(), {})

            second = asyncio.create_task(retry())
            try:
                await second_started.wait()
                assert not first.done(), "La tâche a quitté avant la fin du rollback"
                assert not read.is_set(), "Une autre ingestion est admise pendant le rollback"
                assert listing_ingestion_gate._participants == 2
            finally:
                release_rollback.set()
                if first.done():  # Nettoyage du scénario rouge, sans modifier son assertion.
                    await original_rollback()
                results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert not isinstance(results[1], BaseException)
    assert rollback_completed
    assert not listing_ingestion_gate._entries
    assert listing_ingestion_gate._participants == 0
    assert ((await journey.matches())[0].notified_at is not None) == (trigger == "error")
    if trigger == "cancellation":
        assert (await journey.matches())[0].notification_state == "unknown"


@pytest.mark.parametrize("trigger", ["error", "cancellation", "drain_cancellation"])
@pytest.mark.parametrize("cancel_drain", [False, True])
async def test_drain_preserves_ingestion_rollback_until_sql_finishes(
    journey, monkeypatch, trigger, cancel_drain
):
    """Le drainage attend le rollback protégé, même enregistré après son échéance."""
    from app.core import tasks
    from app.use_cases.ingest_listing import listing_ingestion_gate

    registered = set()
    monkeypatch.setattr(tasks, "_background_tasks", registered)
    monkeypatch.setattr(tasks, "_cleanup_tasks", set())
    entered, rollback_started, release_rollback, drain_observed = (
        asyncio.Event() for _ in range(4)
    )
    rollback_task = None
    rollback_completed = False
    original_wait = asyncio.wait

    async def observe_wait(pending, **kwargs):
        if rollback_task in pending:
            drain_observed.set()  # Les décisions d'annulation de cette vague sont prises.
        return await original_wait(pending, **kwargs)

    monkeypatch.setattr(tasks.asyncio, "wait", observe_wait)
    await journey.add_alert()
    async with journey.database.maker() as session:
        original_rollback = session.rollback

        async def rollback():
            nonlocal rollback_task, rollback_completed
            rollback_task = asyncio.current_task()
            rollback_started.set()
            await release_rollback.wait()
            await original_rollback()
            rollback_completed = True

        async def interrupt_commit():
            entered.set()
            if trigger == "error":
                raise RuntimeError("Échec factice avant persistance")
            await asyncio.Event().wait()

        monkeypatch.setattr(session, "rollback", rollback)
        monkeypatch.setattr(session, "commit", interrupt_commit)
        first = tasks.safe_create_task(
            ProcessListingUseCase(session, journey.bot).execute(journey.payload(), {}),
            name="synthetic-ingestion",
        )
        await asyncio.wait_for(entered.wait(), 3)
        if trigger != "drain_cancellation":
            if trigger == "cancellation":
                first.cancel()
            await rollback_started.wait()
        draining = asyncio.create_task(tasks.wait_all_background_tasks(timeout=0))
        try:
            await rollback_started.wait()
            await drain_observed.wait()
            assert rollback_task.cancelling() == 0, "Le drainage annule directement le rollback"
            assert not first.done()
            assert not draining.done()
            assert session.in_transaction()
            entry = listing_ingestion_gate._entries[("vinted", "console-1")]
            assert entry.lock.locked(), "Protection libérée avant la fin SQL"
            if cancel_drain:
                draining.cancel()
            release_rollback.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            if cancel_drain:
                with pytest.raises(asyncio.CancelledError):
                    await draining
            else:
                await draining
            assert rollback_completed
            assert not session.in_transaction()
            assert not listing_ingestion_gate._entries
            assert not registered
            assert not tasks._cleanup_tasks
            assert journey.deliveries == []
            assert await journey.matches() == []
            assert (await session.execute(select(Listing))).scalars().all() == []
        finally:
            release_rollback.set()
            await asyncio.gather(first, draining, return_exceptions=True)


@pytest.mark.parametrize("failure", ["commit", "timeout"])
async def test_ambiguous_delivery_remains_unknown_without_duplicate(journey, monkeypatch, failure):
    """La protection ne transforme pas une réponse perdue ou un commit échoué en succès."""
    from app.tasks.vinted_poller import load_initial_seen_ids
    from app.use_cases.ingest_listing import listing_ingestion_gate

    await journey.add_alert()

    async def send(chat_id: int, **kwargs):
        journey.deliveries.append((chat_id, kwargs["text"]))
        if failure == "timeout" and len(journey.deliveries) == 1:
            raise TimeoutError("Réponse factice perdue après acceptation")
        return telegram_message(chat_id, len(journey.deliveries))

    journey.bot.send_message.side_effect = send
    if failure == "commit":
        async with journey.database.maker() as session:
            original_commit = session.commit
            commits = 0

            async def commit():
                nonlocal commits
                commits += 1
                if commits == 3:
                    raise RuntimeError("Échec factice avant commit du marquage")
                await original_commit()

            monkeypatch.setattr(session, "commit", commit)
            with pytest.raises(RuntimeError, match="Échec factice avant commit"):
                await ProcessListingUseCase(session, journey.bot).execute(journey.payload(), {})
            assert not session.in_transaction()
    else:
        assert not (await journey.ingest()).needs_retry
    assert not listing_ingestion_gate._entries
    assert (await journey.matches())[0].notified_at is None
    monkeypatch.setattr("app.tasks.vinted_poller.AsyncSessionLocal", journey.database.maker)
    assert "console-1" in await load_initial_seen_ids()
    assert (await journey.ingest()).matches == []
    assert len(journey.deliveries) == 1
    assert (await journey.matches())[0].notified_at is None
    assert (await journey.matches())[0].notification_state == "unknown"


@pytest.mark.parametrize("reason", ["saturated", "timeout"])
async def test_api_admission_failure_is_visible_retryable_and_has_no_side_effect(
    journey, monkeypatch, caplog, reason
):
    """Sans mapping 503, l'API masque la possibilité de rejouer une admission refusée."""
    gate = ListingIngestionGate(max_participants=1 if reason == "saturated" else 2, wait_timeout=0)
    monkeypatch.setattr("app.use_cases.ingest_listing.listing_ingestion_gate", gate)
    await journey.add_alert()
    app = FastAPI()
    app.include_router(router)

    async def db():
        async with journey.database.maker() as session:
            yield session

    app.dependency_overrides[get_db] = db
    async with gate.protect("vinted", "console-1"):
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            result = await client.post(
                "/api/v1/ingest/listings",
                json=journey.payload().model_dump(mode="json"),
                headers={"Authorization": f"Bearer {settings.INGEST_API_TOKEN.get_secret_value()}"},
            )
        assert result.status_code == 503
        assert result.headers["Retry-After"] == "1"
        assert "réessayez" in result.json()["detail"]
        assert any(
            getattr(record, "event", None) == "ingestion_busy" and record.reason == reason
            for record in caplog.records
        )
        assert gate._participants == 1
    assert not gate._entries
    assert journey.deliveries == []
    assert await journey.matches() == []
    async with journey.database.maker() as session:
        assert (await session.execute(select(Listing))).scalars().all() == []


async def test_api_and_poller_share_the_same_listing_protection(journey, monkeypatch):
    """L'entrée HTTP et le poller ne peuvent pas avoir chacun leur propre verrou."""
    from app.services.vinted_scraper import ScrapedVintedItem
    from app.tasks.vinted_poller import _ingest_scraped_item

    entered, release, second_attempt = (asyncio.Event() for _ in range(3))

    class ObservedGate(ListingIngestionGate):
        calls = 0

        @asynccontextmanager
        async def protect(self, source_name, external_id):
            self.calls += 1
            if self.calls == 2:
                second_attempt.set()
            async with super().protect(source_name, external_id):
                yield

    gate = ObservedGate()
    monkeypatch.setattr("app.use_cases.ingest_listing.listing_ingestion_gate", gate)
    monkeypatch.setattr("app.tasks.vinted_poller.AsyncSessionLocal", journey.database.maker)
    monkeypatch.setattr("app.tasks.vinted_poller._ingest_semaphore", None)
    await journey.add_alert()

    async def send(chat_id: int, **kwargs):
        journey.deliveries.append((chat_id, kwargs["text"]))
        entered.set()
        await release.wait()
        return telegram_message(chat_id)

    journey.bot.send_message.side_effect = send
    app = FastAPI()
    app.include_router(router)

    async def db():
        async with journey.database.maker() as session:
            yield session

    app.dependency_overrides[get_db] = db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(
            client.post(
                "/api/v1/ingest/listings",
                json=journey.payload().model_dump(mode="json"),
                headers={"Authorization": f"Bearer {settings.INGEST_API_TOKEN.get_secret_value()}"},
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        item = ScrapedVintedItem(
            external_id="console-1",
            title="Console de jeu",
            item_price_cents=1000,
            url="https://example.invalid/console-1",
            condition="Très bon état",
            photo_url=None,
        )
        second = asyncio.create_task(_ingest_scraped_item(item, "tech"))
        try:
            await second_attempt.wait()
            assert gate._participants == 2
            assert not second.done()
        finally:
            release.set()
            first_result, second_result = await asyncio.gather(first, second)
    assert first_result.status_code == 200
    assert second_result.notified_count == 0
    assert not second_result.needs_retry
    assert len(journey.deliveries) == 1
    assert not gate._entries


@pytest.mark.parametrize("reason", ["saturated", "timeout"])
async def test_poller_admission_failure_discards_seen_id_and_reports_failure(
    journey, monkeypatch, caplog, reason
):
    """Un refus d'admission ne peut pas laisser l'annonce marquée vue dans le poller."""
    from app.services.vinted_scraper import ScrapedVintedItem
    from app.tasks.vinted_poller import FifoSet, poll_vinted_once

    gate = ListingIngestionGate(max_participants=1 if reason == "saturated" else 2, wait_timeout=0)
    monkeypatch.setattr("app.use_cases.ingest_listing.listing_ingestion_gate", gate)
    monkeypatch.setattr("app.tasks.vinted_poller.AsyncSessionLocal", journey.database.maker)
    monkeypatch.setattr("app.tasks.vinted_poller._ingest_semaphore", None)
    item = ScrapedVintedItem(
        external_id="console-1",
        title="Console de jeu",
        item_price_cents=1000,
        url="https://example.invalid/console-1",
        condition="Très bon état",
        photo_url=None,
    )
    monkeypatch.setattr("app.tasks.vinted_poller.fetch_vinted_page", AsyncMock(return_value=[item]))
    seen = FifoSet()
    async with gate.protect("vinted", "console-1"):
        assert await poll_vinted_once(seen, vinted_client=AsyncMock(), category_slug="tech") == 0
        assert item.external_id not in seen
        assert any("réessayez" in record.getMessage() for record in caplog.records)
        assert gate._participants == 1
    assert not gate._entries
    assert journey.deliveries == []
    assert await journey.matches() == []


async def test_new_watch_suppresses_baseline_even_for_globally_seen_items(journey):
    from datetime import UTC, datetime

    from app.db.repositories.alert_repo import AlertRepository

    legacy = await journey.add_alert()
    await journey.ingest("known")
    async with journey.database.maker() as session:
        new = await AlertRepository(session).create(
            user_id=legacy.user_id, category_id=journey.category.id,
            name="Console nouvelle", include_keywords=["console"],
            vinted_search_url="https://example.invalid/search",
        )
        await session.commit()
        started = datetime.now(UTC)
        await AlertRepository(session).initialize_listing_baselines(
            "tech", "https://example.invalid/search", ["known", "old"], started,
        )
        await session.commit()
        new_id = new.id
    await journey.ingest("known")
    await journey.ingest("old")
    assert len(journey.deliveries) == 2  # L'ancienne alerte garde son comportement.
    await journey.ingest("new")
    matches = await journey.matches()
    assert sum(row.alert_id == new_id for row in matches) == 1
    assert len(journey.deliveries) == 4
    async with journey.database.maker() as session:
        reread = await session.get(Alert, new_id)
        assert reread.listing_baseline["state"] == "ready"
        await AlertRepository(session).initialize_listing_baselines(
            "tech", "https://example.invalid/search", ["new"], datetime.now(UTC),
        )
        await session.commit()
        assert reread.listing_baseline["external_ids"] == ["known", "old"]


async def test_pending_watch_blocks_ingestion_and_failed_scan_keeps_it_pending(
    journey, monkeypatch,
):
    from app.db.repositories.alert_repo import AlertRepository
    from app.tasks import vinted_poller as poller

    legacy = await journey.add_alert()
    async with journey.database.maker() as session:
        new = await AlertRepository(session).create(
            user_id=legacy.user_id, category_id=journey.category.id,
            name="Console nouvelle", include_keywords=["console"],
        )
        await session.commit()
        new_id = new.id
    await journey.ingest("pending")
    assert all(row.alert_id != new_id for row in await journey.matches())
    monkeypatch.setattr(poller, "AsyncSessionLocal", journey.database.maker)
    monkeypatch.setattr(poller, "fetch_vinted_page", AsyncMock(side_effect=TimeoutError()))
    with pytest.raises(TimeoutError):
        await poller.poll_vinted_once(set(), AsyncMock(), "https://example.invalid/search", "tech")
    async with journey.database.maker() as session:
        assert (await session.get(Alert, new_id)).listing_baseline["state"] == "pending"


async def test_known_publication_before_creation_cannot_notify(journey):
    from datetime import UTC, datetime, timedelta

    from app.db.repositories.alert_repo import AlertRepository

    legacy = await journey.add_alert()
    async with journey.database.maker() as session:
        await AlertRepository(session).create(
            user_id=legacy.user_id, category_id=journey.category.id,
            name="Console nouvelle", include_keywords=["console"],
            vinted_search_url="https://example.invalid/search",
        )
        await session.commit()
        await AlertRepository(session).initialize_listing_baselines(
            "tech", "https://example.invalid/search", [], datetime.now(UTC),
        )
        await session.commit()
        payload = journey.payload("old-publication")
        payload.published_at = datetime.now(UTC)-timedelta(days=1)
        await ProcessListingUseCase(session, journey.bot).execute(payload, {})
    assert len(journey.deliveries) == 1


async def test_initial_catalogue_is_abandoned_by_notification_recovery(journey, monkeypatch):
    from datetime import UTC, datetime

    from app.db.repositories.alert_repo import AlertRepository
    from app.domain.enums import NotificationState

    legacy = await journey.add_alert()
    await journey.ingest("baseline")
    async with journey.database.maker() as session:
        alert = await AlertRepository(session).create(
            user_id=legacy.user_id, category_id=journey.category.id,
            name="Console nouvelle", include_keywords=["console"],
            vinted_search_url="https://example.invalid/search",
        )
        listing = await ListingRepository(session).get_by_source_and_external_id(
            "vinted", "baseline",
        )
        await ListingRepository(session).create_match(alert.id, listing.id, known_unsent=True)
        await session.commit()
        await AlertRepository(session).initialize_listing_baselines(
            "tech", "https://example.invalid/search", ["baseline"], datetime.now(UTC),
        )
        await session.commit()
        from app.tasks import recover_notifications as recovery
        monkeypatch.setattr(recovery, "AsyncSessionLocal", journey.database.maker)
        monkeypatch.setattr(recovery, "get_bot", lambda: journey.bot)
        await recovery._recover_notifications()
        row = await session.scalar(select(ListingMatch).where(
            ListingMatch.alert_id == alert.id,
        ).execution_options(populate_existing=True))
        assert row.notification_state == NotificationState.ABANDONED
        assert row.notification_reason == "initial_catalogue"
    assert len(journey.deliveries) == 1
