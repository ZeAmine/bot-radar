"""Une préparation BD est durable et respecte les intentions de l'utilisateur."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from app.bot.handlers.alerts_create import save_alert
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.user import User
from app.domain.entities.alert_preparation import PreparationRequest
from app.domain.enums import AlertStatus
from app.providers.base import MarketOffers
from app.services.book_metadata_service import BookMetadata, CatalogueVerdict
from tests.add_fakes import DraftState as _DraftState
from tests.database import postgres_replayed_database
from tests.screens import confirm_press


class DraftState(_DraftState):
    """Identité FSM locale, sans modifier le double partagé."""

    def __init__(self):
        super().__init__()
        self.storage = MemoryStorage()
        self.key = StorageKey(bot_id=1, chat_id=42, user_id=42)


@pytest.mark.parametrize("recover", [False, True])
async def test_skipped_one_shot_is_linked_and_priced_from_its_durable_request(
    preparation, monkeypatch, recover,
):
    from app.bot.handlers.alerts_create_confirm import process_creation_edit_input
    from app.core.constants import ProviderQueryStatus
    from app.db.repositories.book_repo import BookRepository
    from app.providers.base import CollectionAttempt, MarketOffer
    from app.providers.google_books import ProviderQueryResult
    from app.services.book_metadata_service import BookMetadataService
    from tests.screens import incoming_message

    p = preparation
    meta = BookMetadata(
        title="Jours de sable", author="Aimée de Jongh", publisher="Dargaud Benelux",
        isbn13="9782505082545", language="fr",
    )
    monkeypatch.setattr(BookMetadataService, "_bnf_records_result", AsyncMock(return_value=(
        ProviderQueryResult(status=ProviderQueryStatus.SUCCESS, items=[meta]), None,
    )))
    p.prices.return_value = MarketOffers([
        MarketOffer("synthetic", 2059, 0, 2059, "https://example.invalid/offer"),
    ], attempts=(CollectionAttempt("synthetic"),))
    state = DraftState()
    state.data = {
        "name": meta.title, "isbn": meta.isbn13, "author": meta.author,
        "is_series": False, "category_slug": "bd_comics",
        "include_keywords": ["jours de sable"], "current_edit_field": "isbn",
        "max_item_price_cents": 3000,
    }
    with patch("app.bot.handlers.alerts_create_confirm.relaunch_keyword_search"):
        await process_creation_edit_input(incoming_message("/skip"), state)
    assert state.data["isbn"] is None
    callback, _ = confirm_press()
    saved_requests = []

    async def prepare(alert_id, **options):
        persisted = await p.read(alert_id)
        assert persisted.status == AlertStatus.PAUSED
        saved_requests.append(persisted.preparation)
        if recover:
            raise asyncio.CancelledError
        return await p.module.prepare_bd_alert(alert_id, **options)

    monkeypatch.setattr("app.bot.handlers.alerts_create.prepare_bd_alert", prepare)
    async with p.db.maker() as session:
        user = (await session.execute(select(User))).scalar_one()
        if recover:
            with pytest.raises(asyncio.CancelledError):
                await save_alert(callback, state, user, session)
        else:
            await save_alert(callback, state, user, session)
    assert not state.data
    if recover:
        monkeypatch.setattr(p.module, "preparation_runner", p.module.PreparationRunner())
        await p.module.recover_bd_preparations()
    async with p.db.maker() as session:
        alert = (await session.execute(select(Alert))).scalar_one()
        books = await BookRepository(session).get_all_books_for_alert(alert.id)
        assert [book.isbn13 for book in books] == ["9782505082545"]
        assert books[0].market_cache.lowest_delivered_price_cents == 2059
        assert books[0].market_cache.collection_status == "complete"
        assert alert.status == AlertStatus.ACTIVE
        assert alert.preparation is None
    assert saved_requests[0]["bibliographic_mode"] == "one_shot"
    p.lookup.assert_not_awaited()


@pytest.mark.parametrize("mode", ["one_shot", "series", None, "missing"])
async def test_durable_mode_survives_restart_and_selects_the_same_catalogue_path(
    preparation, monkeypatch, mode,
):
    from app.db.repositories.alert_repo import AlertRepository
    from app.db.repositories.book_repo import BookRepository
    from app.services.book_metadata_service import BookMetadataService

    p = preparation
    alert_id = await p.create()
    expected_isbn = "9782505082545" if mode == "one_shot" else "9782369904151"
    notice = BookMetadata(
        title="Jours de sable" if mode == "one_shot" else "Série synthétique Tome 1",
        isbn13=expected_isbn,
    )
    verdict = CatalogueVerdict(consulted=("bnf",), notices=(notice,))
    single = AsyncMock(return_value=verdict)
    monkeypatch.setattr(BookMetadataService, "lookup_by_title_and_author", single)
    p.lookup.return_value = verdict
    async with p.db.maker() as session:
        repo = AlertRepository(session)
        alert = await repo.get_by_id(alert_id)
        request = dict(alert.preparation)
        if mode == "missing":
            request.pop("bibliographic_mode", None)
        else:
            request["bibliographic_mode"] = mode
        alert.preparation = request
        old_token = request["token"]
        await session.commit()
        await repo.set_status(alert, AlertStatus.PAUSED)
        await repo.set_status(alert, AlertStatus.ACTIVE)
        await session.commit()
    persisted = (await p.read(alert_id)).preparation
    assert persisted["token"] != old_token
    assert persisted["bibliographic_mode"] == (None if mode == "missing" else mode)
    monkeypatch.setattr(p.module, "preparation_runner", p.module.PreparationRunner())
    await p.module.recover_bd_preparations()
    async with p.db.maker() as session:
        books = await BookRepository(session).get_all_books_for_alert(alert_id)
        assert [book.isbn13 for book in books] == [expected_isbn]
        assert books[0].market_cache.collection_status == "empty"
    assert (await p.read(alert_id)).status == AlertStatus.ACTIVE
    assert single.await_count == (1 if mode == "one_shot" else 0)
    assert p.lookup.await_count == (0 if mode == "one_shot" else 1)


async def test_old_one_shot_preparation_cannot_link_or_activate_a_replacement(
    preparation, monkeypatch,
):
    from app.db.repositories.alert_repo import AlertRepository
    from app.db.repositories.book_repo import BookRepository
    from app.services.book_metadata_service import BookMetadataService

    p = preparation
    alert_id = await p.create(name="Ancien one-shot")
    async with p.db.maker() as session:
        alert = await session.get(Alert, alert_id)
        alert.preparation = PreparationRequest(bibliographic_mode="one_shot").stored()
        old_token = alert.preparation["token"]
        await session.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(_service, _query):
        entered.set()
        await release.wait()
        return CatalogueVerdict(notices=(BookMetadata(
            title="Ancien one-shot", isbn13="9782505082545",
        ),), consulted=("bnf",))

    monkeypatch.setattr(BookMetadataService, "lookup_by_title_and_author", lookup)
    async with asyncio.timeout(3):
        task = asyncio.create_task(p.module.preparation_runner._run(alert_id))
        await entered.wait()
        try:
            async with p.db.maker() as session:
                repo = AlertRepository(session)
                alert = await repo.get_by_id(alert_id)
                await repo.lock_for_edit(alert)
                alert.name = "Nouvelle œuvre"
                await repo.invalidate_preparation(alert, author_hint=None)
                await session.commit()
        finally:
            release.set()
        outcome = await task
    assert outcome.status == "superseded"
    current = await p.read(alert_id)
    assert current.name == "Nouvelle œuvre"
    assert current.status == AlertStatus.PAUSED
    assert current.preparation["token"] != old_token
    assert current.preparation["bibliographic_mode"] == "one_shot"
    async with p.db.maker() as session:
        assert not await BookRepository(session).get_all_books_for_alert(alert_id)
    p.prices.assert_not_awaited()


@pytest.mark.parametrize("isbn", [None, "9782369904151"])
async def test_first_commit_contains_a_resumable_request_before_fsm_clear(isbn):
    """Sans marqueur, l'interruption après commit est indiscernable d'une pause volontaire."""
    async with postgres_replayed_database() as db:
        db.maker.configure(autoflush=False)
        category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        user = User(telegram_user_id=601, is_allowed=True)
        db.session.add_all([category, user])
        await db.session.commit()
        state = DraftState()
        state.data = {
            "name": "Livre synthétique",
            "isbn": isbn,
            "author": "Auteur demandé",
            "category_slug": "bd_comics",
            "category_id": str(category.id),
            "include_keywords": ["livre"],
        }
        callback, _ = confirm_press()

        async def interrupted_clear():
            raise asyncio.CancelledError()

        with patch.object(state, "clear", new=interrupted_clear):
            async with db.maker() as session:
                with pytest.raises(asyncio.CancelledError):
                    await save_alert(callback, state, user, session)
        async with db.maker() as session:
            alert = (await session.execute(select(Alert))).scalar_one()
            assert alert.status == AlertStatus.PAUSED
            request = getattr(alert, "preparation", None)
            assert request is not None, "Le premier commit ne porte aucune demande de préparation"
            assert request["state"] == "pending"
            assert request["activation_requested"] is True
            assert request["author_hint"] == "Auteur demandé"


@pytest.fixture
async def preparation(monkeypatch):
    from app.use_cases import prepare_bd_alert as module

    async with postgres_replayed_database() as db:
        db.maker.configure(autoflush=False)
        category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        user = User(telegram_user_id=601, is_allowed=True)
        db.session.add_all([category, user])
        await db.session.commit()
        lookup = AsyncMock(return_value=CatalogueVerdict(consulted=("BNF",)))
        prices = AsyncMock(return_value=MarketOffers())
        monkeypatch.setattr("app.db.session.AsyncSessionLocal", db.maker)
        monkeypatch.setattr("app.services.book_market_pricing.AsyncSessionLocal", db.maker)
        monkeypatch.setattr(module, "AsyncSessionLocal", db.maker)
        monkeypatch.setattr(module, "preparation_runner", module.PreparationRunner())
        monkeypatch.setattr(
            "app.services.book_metadata_service.BookMetadataService.lookup_series_notices", lookup
        )
        monkeypatch.setattr("app.services.book_market_pricing.fetch_offers_for_book", prices)

        async def create(name="Série synthétique", author="Auteur demandé"):
            async with db.maker() as session:
                alert = Alert(
                    user_id=user.id,
                    category_id=category.id,
                    name=name,
                    include_keywords=[name],
                    status=AlertStatus.PAUSED,
                    preparation=PreparationRequest(author_hint=author).stored(),
                )
                session.add(alert)
                await session.commit()
                return alert.id

        async def read(alert_id):
            async with db.maker() as session:
                return await session.get(Alert, alert_id)

        yield SimpleNamespace(
            db=db, create=create, read=read, lookup=lookup, prices=prices, module=module
        )


@pytest.mark.parametrize("unavailable", [False, True])
async def test_textual_preparation_completes_without_catalogue_or_price(preparation, unavailable):
    """Un ISBN et une cote ne sont pas des conditions d'activation d'une veille textuelle."""
    p = preparation
    p.lookup.return_value = CatalogueVerdict(
        consulted=("BNF",), unavailable=("BNF",) if unavailable else ()
    )
    alert_id = await p.create()
    before_version = (await p.read(alert_id)).decision_version
    assert await p.module.prepare_bd_alert(alert_id)
    alert = await p.read(alert_id)
    assert alert.status == AlertStatus.ACTIVE
    assert alert.preparation is None
    assert alert.decision_version == before_version + 1
    assert (alert.book_search_exhausted_at is not None) == (not unavailable)


@pytest.mark.parametrize("action", [None, "pause", "delete"])
async def test_edition_edit_joins_durable_preparation_and_preserves_current_intent(
    preparation, monkeypatch, action
):
    """L'intégration #81 conserve le jeton F04 et la confiance de la fiche choisie."""
    from app.bot.handlers import alerts_manage
    from app.db.models.alert_book import AlertBook
    from app.db.repositories.alert_repo import AlertRepository
    from app.domain.enums import MatchConfidence

    p = preparation
    alert_id = await p.create(name="Édition synthétique")
    initial = (await p.read(alert_id)).preparation["token"]
    catalog_entered, catalog_release = asyncio.Event(), asyncio.Event()
    price_entered, price_release = asyncio.Event(), asyncio.Event()
    edit_committed = asyncio.Event()

    async def wait_event(event, *operations):
        signal = asyncio.create_task(event.wait())
        done, _ = await asyncio.wait(
            [signal, *operations], return_when=asyncio.FIRST_COMPLETED
        )
        if not event.is_set():
            signal.cancel()
            for operation in done:
                await operation
            pytest.fail("Le traitement s'est terminé avant le jalon attendu")
        await signal

    async def catalogue(*_args, **_kwargs):
        catalog_entered.set()
        await catalog_release.wait()
        return CatalogueVerdict(
            consulted=("BNF",),
            notices=(BookMetadata(title="Ancien tome", isbn13="9782369903086"),),
        )

    async def pricing(*_args, **_kwargs):
        price_entered.set()
        await price_release.wait()
        return MarketOffers()

    p.lookup.side_effect = catalogue
    p.prices.side_effect = pricing
    monkeypatch.setattr(alerts_manage, "AsyncSessionLocal", p.db.maker)
    monkeypatch.setattr(alerts_manage, "safe_create_task", lambda coro, **_kwargs: coro.close())
    monkeypatch.setattr(
        alerts_manage, "_lookup_isbn_notice", AsyncMock(return_value=BookMetadata(
            title="Édition synthétique", isbn13="9782369904151",
            confidence=MatchConfidence.LOW.value,
        ))
    )
    state = DraftState()
    state.data = {"edit_alert_id": str(alert_id), "edit_field": "isbn"}
    clear = state.clear

    async def committed_clear():
        await clear()
        edit_committed.set()

    state.clear = committed_clear
    message = AsyncMock()
    message.text = "9782369904151"
    first = asyncio.create_task(p.module.prepare_bd_alert(alert_id))
    await catalog_entered.wait()
    async with p.db.maker() as session:
        user = (await session.execute(select(User))).scalar_one()
        edited = asyncio.create_task(alerts_manage.save_edited_field_value(
            message, state, user, session
        ))
        await wait_event(edit_committed, edited)
        current = await p.read(alert_id)
        assert current.preparation["token"] != initial
        assert current.isbn == "9782369904151"
        catalog_release.set()
        await wait_event(price_entered, edited, first)
        if action:
            async with p.db.maker() as gesture:
                repo = AlertRepository(gesture)
                current = await repo.get_by_id(alert_id)
                if action == "pause":
                    await repo.set_status(current, AlertStatus.PAUSED)
                else:
                    await repo.soft_delete(current)
                await gesture.commit()
        price_release.set()
        await edited
        await first
    current = await p.read(alert_id)
    assert current.status == {
        None: AlertStatus.ACTIVE, "pause": AlertStatus.PAUSED, "delete": AlertStatus.DELETED
    }[action]
    assert (current.preparation is None) == (action is None)
    p.prices.assert_awaited_once()
    async with p.db.maker() as session:
        links = list((await session.execute(select(AlertBook))).scalars())
        assert len(links) == 1
        assert links[0].match_confidence == MatchConfidence.LOW


@pytest.mark.parametrize("action", ["pause", "delete", "edit"])
async def test_old_catalogue_result_cannot_attach_or_activate_after_user_action(
    preparation, action
):
    from app.db.models.alert_book import AlertBook
    from app.db.repositories.alert_repo import AlertRepository

    p = preparation
    alert_id = await p.create()
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(title, **_kwargs):
        if title == "Série synthétique":
            entered.set()
            await release.wait()
            return CatalogueVerdict(
                consulted=("BNF",),
                notices=(BookMetadata(title="Ancien tome", isbn13="9782369904151"),),
            )
        return CatalogueVerdict(consulted=("BNF",))

    p.lookup.side_effect = lookup
    task = asyncio.create_task(p.module.prepare_bd_alert(alert_id))
    await entered.wait()
    async with p.db.maker() as session:
        repo = AlertRepository(session)
        alert = await repo.get_by_id(alert_id)
        if action == "pause":
            await repo.set_status(alert, AlertStatus.PAUSED)
        elif action == "delete":
            await repo.delete_completely(alert)
        else:
            await repo.lock_for_edit(alert)
            alert.name = "Nouvelle série"
            await repo.invalidate_preparation(alert, author_hint=None)
        await session.commit()
    release.set()
    result = await task
    alert = await p.read(alert_id)
    if action == "delete":
        assert alert is None
        assert not result
    elif action == "pause":
        assert alert.status == AlertStatus.PAUSED
        assert not result
        assert alert.preparation["activation_requested"] is False
    else:
        assert result
        assert alert.status == AlertStatus.ACTIVE
        assert alert.name == "Nouvelle série"
    async with p.db.maker() as session:
        assert (await session.execute(select(AlertBook))).scalars().all() == []


@pytest.mark.parametrize("phase", ["catalog_write", "pricing_write", "activation_write"])
async def test_write_failure_blocks_until_explicit_resume(preparation, monkeypatch, phase):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models.book_market_cache import BookMarketCache
    from app.db.repositories.alert_repo import AlertRepository
    from app.db.repositories.book_repo import BookRepository
    from app.services import book_market_pricing

    p = preparation
    p.lookup.return_value = CatalogueVerdict(
        consulted=("BNF",),
        notices=(BookMetadata(title="Tome synthétique", isbn13="9782369904151"),),
    )
    alert_id = await p.create()
    original_commit = AsyncSession.commit
    original_link = BookRepository.link_alert_to_book
    original_persist = book_market_pricing.persist_market_result
    fail = True

    async def link(repo, *args, **kwargs):
        result = await original_link(repo, *args, **kwargs)
        if phase == "catalog_write":
            repo.session.info["fail_preparation_commit"] = True
        return result

    async def persist(session, *args, **kwargs):
        result = await original_persist(session, *args, **kwargs)
        if phase == "pricing_write":
            session.info["fail_preparation_commit"] = True
        return result

    async def commit(session):
        nonlocal fail
        activation = any(
            isinstance(row, Alert) and row.status == AlertStatus.ACTIVE for row in session.dirty
        )
        if fail and (
            session.info.pop("fail_preparation_commit", False)
            or (phase == "activation_write" and activation)
        ):
            fail = False
            raise RuntimeError("Erreur d'écriture synthétique")
        await original_commit(session)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(BookRepository, "link_alert_to_book", link)
    monkeypatch.setattr(book_market_pricing, "persist_market_result", persist)
    assert not await p.module.prepare_bd_alert(alert_id)
    blocked = await p.read(alert_id)
    assert blocked.status == AlertStatus.PAUSED
    assert blocked.preparation["state"] == "blocked"
    assert blocked.preparation["error_code"] == phase
    assert "Erreur d'écriture" not in str(blocked.preparation)
    calls = p.lookup.await_count
    await p.module.recover_bd_preparations()
    assert p.lookup.await_count == calls
    async with p.db.maker() as session:
        repo = AlertRepository(session)
        alert = await repo.get_by_id(alert_id)
        await repo.set_status(alert, AlertStatus.ACTIVE)
        assert alert.status == AlertStatus.PAUSED
        assert alert.preparation["token"] != blocked.preparation["token"]
        await session.commit()
    assert await p.module.prepare_bd_alert(alert_id)
    assert (await p.read(alert_id)).preparation is None
    async with p.db.maker() as session:
        assert len((await session.execute(select(BookMarketCache))).scalars().all()) == 1


async def test_empty_price_is_a_completed_attempt_not_a_blocking_error(preparation):
    from app.db.models.book_market_cache import BookMarketCache

    p = preparation
    p.lookup.return_value = CatalogueVerdict(
        consulted=("BNF",),
        notices=(BookMetadata(title="Tome synthétique", isbn13="9782369904151"),),
    )
    alert_id = await p.create()
    assert await p.module.prepare_bd_alert(alert_id)
    assert (await p.read(alert_id)).status == AlertStatus.ACTIVE
    async with p.db.maker() as session:
        cache = (await session.execute(select(BookMarketCache))).scalar_one()
        assert cache.collection_status == "empty"
        assert cache.checked_at is not None


async def test_recovery_limits_each_pass_and_all_network_preparations_to_two(preparation):
    p = preparation
    ids = [await p.create(f"Série {index}") for index in range(6)]
    entered, release = asyncio.Event(), asyncio.Event()
    active = peak = calls = 0

    async def lookup(*_args, **_kwargs):
        nonlocal active, peak, calls
        active += 1
        calls += 1
        peak = max(peak, active)
        if active == 2:
            entered.set()
        await release.wait()
        active -= 1
        return CatalogueVerdict(consulted=("BNF",))

    p.lookup.side_effect = lookup
    first = asyncio.create_task(p.module.recover_bd_preparations())
    await entered.wait()
    assert calls == 2
    await p.module.recover_bd_preparations()  # Une passe déjà en cours ne se chevauche pas.
    assert calls == 2
    release.set()
    await first
    assert peak == 2
    assert calls == 4
    observed = [await p.read(identifier) for identifier in ids]
    assert sum(alert.status == AlertStatus.ACTIVE for alert in observed) == 4
    await p.module.recover_bd_preparations()
    assert calls == 6


async def test_interrupted_requests_do_not_starve_the_following_requests(preparation):
    p = preparation
    ids = [await p.create(f"Série {index}") for index in range(6)]

    async def interrupted(*_args, **_kwargs):
        raise asyncio.CancelledError()

    p.lookup.side_effect = interrupted
    await p.module.recover_bd_preparations()
    async with p.db.maker() as session:
        from app.db.repositories.alert_repo import AlertRepository

        following = await AlertRepository(session).pending_preparation_ids(set())
    assert following[:2] == ids[4:]
    p.lookup.side_effect = None
    await p.module.recover_bd_preparations()
    assert (await p.read(ids[4])).status == AlertStatus.ACTIVE
    assert (await p.read(ids[5])).status == AlertStatus.ACTIVE


async def test_legacy_pauses_and_voluntary_pauses_are_never_recovered(preparation):
    from app.db.repositories.alert_repo import AlertRepository

    p = preparation
    paused_id = await p.create()
    blocked_id = await p.create("Bloquée")
    async with p.db.maker() as session:
        repo = AlertRepository(session)
        paused = await repo.get_by_id(paused_id)
        await repo.set_status(paused, AlertStatus.PAUSED)
        blocked = await repo.get_by_id(blocked_id)
        request = PreparationRequest.model_validate(blocked.preparation)
        request.state, request.error_code = "blocked", "catalog_write"
        blocked.preparation = request.stored()
        legacy = Alert(
            user_id=paused.user_id,
            category_id=paused.category_id,
            name="Ancienne pause",
            include_keywords=[],
            status=AlertStatus.PAUSED,
        )
        session.add(legacy)
        await session.commit()
        legacy_id = legacy.id
    await p.module.recover_bd_preparations()
    p.lookup.assert_not_awaited()
    for identifier in (legacy_id, paused_id, blocked_id):
        assert (await p.read(identifier)).status == AlertStatus.PAUSED
    async with p.db.maker() as session:
        repo = AlertRepository(session)
        legacy = await repo.get_by_id(legacy_id)
        await repo.set_status(legacy, AlertStatus.ACTIVE)
        await session.commit()
    assert (await p.read(legacy_id)).status == AlertStatus.ACTIVE
    assert (await p.read(legacy_id)).preparation is None


@pytest.mark.parametrize("phase", ["catalogue", "pricing", "activation"])
async def test_interrupted_worker_is_recovered_without_its_old_caller(
    preparation, monkeypatch, phase
):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services import book_market_pricing

    p = preparation
    alert_id = await p.create()
    entered, release = asyncio.Event(), asyncio.Event()
    original_commit = AsyncSession.commit

    async def lookup(*_args, **_kwargs):
        if phase == "catalogue":
            entered.set()
            await release.wait()
        return CatalogueVerdict(
            consulted=("BNF",),
            notices=(BookMetadata(title="Tome synthétique", isbn13="9782369904151"),),
        )

    async def prices(*_args, **_kwargs):
        if phase == "pricing":
            entered.set()
            await release.wait()
        return MarketOffers()

    async def commit(session):
        if phase == "activation" and any(
            isinstance(row, Alert) and row.status == AlertStatus.ACTIVE for row in session.dirty
        ):
            entered.set()
            await release.wait()
        await original_commit(session)

    p.lookup.side_effect, p.prices.side_effect = lookup, prices
    monkeypatch.setattr(AsyncSession, "commit", commit)
    caller = asyncio.create_task(p.module.prepare_bd_alert(alert_id))
    await entered.wait()
    worker = p.module.preparation_runner.tasks[alert_id]
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert (await p.read(alert_id)).status == AlertStatus.PAUSED
    assert (await p.read(alert_id)).preparation["state"] == "pending"
    release.set()
    # Une cotation engagée peut finir, mais elle ne termine pas la préparation.
    await asyncio.gather(*list(book_market_pricing._pricing_tasks.values()), return_exceptions=True)
    assert (await p.read(alert_id)).status == AlertStatus.PAUSED
    monkeypatch.setattr(p.module, "preparation_runner", p.module.PreparationRunner())
    await p.module.recover_bd_preparations()
    assert (await p.read(alert_id)).status == AlertStatus.ACTIVE
    assert (await p.read(alert_id)).preparation is None


async def test_non_bibliographic_edit_does_not_restart_preparation(preparation):
    from app.db.repositories.alert_repo import AlertRepository

    p = preparation
    alert_id = await p.create()
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return CatalogueVerdict(consulted=("BNF",))

    p.lookup.side_effect = lookup
    task = asyncio.create_task(p.module.prepare_bd_alert(alert_id))
    await entered.wait()
    before = (await p.read(alert_id)).preparation["token"]
    async with p.db.maker() as session:
        repo = AlertRepository(session)
        alert = await repo.get_by_id(alert_id)
        await repo.lock_for_edit(alert)
        alert.max_item_price_cents = 1234
        await session.commit()
    assert (await p.read(alert_id)).preparation["token"] == before
    release.set()
    assert await task
    assert p.lookup.await_count == 1
    assert (await p.read(alert_id)).max_item_price_cents == 1234


async def test_initial_confirmation_failure_does_not_block_activation(preparation):
    p = preparation
    callback, message = confirm_press()
    message.edit_text.side_effect = RuntimeError("Message synthétique indisponible")
    async with p.db.maker() as session:
        category = (await session.execute(select(Category))).scalar_one()
        user = (await session.execute(select(User))).scalar_one()
        state = DraftState()
        state.data = {
            "name": "Série synthétique",
            "include_keywords": ["serie"],
            "category_id": str(category.id),
            "category_slug": "bd_comics",
        }
        await save_alert(callback, state, user, session)
    async with p.db.maker() as session:
        alert = (await session.execute(select(Alert))).scalar_one()
        assert alert.status == AlertStatus.ACTIVE
        assert alert.preparation is None


@pytest.mark.parametrize("ack_fails", [False, True])
async def test_resume_acknowledges_callback_before_network_and_reloads_the_result(
    preparation, ack_fails
):
    from app.bot.handlers.alerts_manage import toggle_resume_alert

    p = preparation
    alert_id = await p.create()
    async with p.db.maker() as session:
        from app.db.repositories.alert_repo import AlertRepository

        alert = await AlertRepository(session).get_by_id(alert_id)
        await AlertRepository(session).set_status(alert, AlertStatus.PAUSED)
        await session.commit()
        user = (await session.execute(select(User))).scalar_one()
        callback, _message = confirm_press()
        callback.data = f"al_resume:{alert_id}"
        callback.answer.side_effect = [
            RuntimeError("Premier acquittement indisponible") if ack_fails else None,
            RuntimeError("Callback expiré après collecte"),
        ]

        async def lookup(*_args, **_kwargs):
            callback.answer.assert_awaited_once()
            return CatalogueVerdict(consulted=("BNF",))

        p.lookup.side_effect = lookup
        await toggle_resume_alert(callback, user, session)
        callback.answer.assert_awaited_once()
    assert (await p.read(alert_id)).status == AlertStatus.ACTIVE


async def test_scheduler_has_a_due_startup_pass_and_runs_the_same_recovery(preparation):
    from datetime import UTC, datetime, timedelta

    from app.tasks.scheduler import create_scheduler

    p = preparation
    alert_id = await p.create()
    scheduler = create_scheduler()  # Jamais démarré dans ce test.
    job = scheduler.get_job("recover_bd_preparations")
    assert job.trigger.interval == timedelta(minutes=5)
    assert job.next_run_time <= datetime.now(UTC)
    assert job.max_instances == 1
    assert job.coalesce is True
    await job.func()
    assert (await p.read(alert_id)).status == AlertStatus.ACTIVE


async def test_blocked_state_write_failure_never_repeats_provider_work(preparation, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    p = preparation
    alert_id = await p.create()
    p.lookup.side_effect = RuntimeError("Échec technique synthétique")
    original = AsyncSession.commit
    fail = True

    async def commit(session):
        nonlocal fail
        if fail and any(
            isinstance(row, Alert) and row.preparation and row.preparation["state"] == "blocked"
            for row in session.dirty
        ):
            fail = False
            raise RuntimeError("État temporairement non enregistrable")
        await original(session)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    assert not await p.module.prepare_bd_alert(alert_id)
    assert alert_id in p.module.preparation_runner.deferred_blocks
    assert not await p.module.prepare_bd_alert(alert_id)
    assert p.lookup.await_count == 1
    await p.module.recover_bd_preparations()
    assert p.lookup.await_count == 1
    assert (await p.read(alert_id)).preparation["state"] == "blocked"
    assert not p.module.preparation_runner.deferred_blocks


async def test_joiners_follow_a_new_token_created_before_old_work_returns(preparation, monkeypatch):
    from app.db.repositories.alert_repo import AlertRepository

    p = preparation
    alert_id = await p.create()
    entered, release, joined = (asyncio.Event() for _ in range(3))
    calls = 0

    async def lookup(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("Premier travail synthétique bloqué")
        return CatalogueVerdict(consulted=("BNF",))

    runner = p.module.preparation_runner
    original_run = runner._run

    async def run(identifier):
        outcome = await original_run(identifier)
        if outcome.status == "blocked":
            # Relance committée après le résultat ancien, avant le réveil des appelants.
            async with p.db.maker() as session:
                repo = AlertRepository(session)
                alert = await repo.get_by_id(identifier)
                await repo.set_status(alert, AlertStatus.ACTIVE)
                await session.commit()
        return outcome

    async def join():
        joined.set()
        return await p.module.prepare_bd_alert(alert_id)

    p.lookup.side_effect = lookup
    monkeypatch.setattr(runner, "_run", run)
    first = asyncio.create_task(p.module.prepare_bd_alert(alert_id))
    await entered.wait()
    second = asyncio.create_task(join())
    await joined.wait()
    release.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert calls == 2
    assert (await p.read(alert_id)).preparation is None


async def test_two_callers_share_preparation_and_joiner_cancellation_does_not_cancel_it(
    preparation,
):
    p = preparation
    alert_id = await p.create()
    entered, release, joined = (asyncio.Event() for _ in range(3))

    async def lookup(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return CatalogueVerdict(consulted=("BNF",))

    async def join():
        joined.set()
        return await p.module.prepare_bd_alert(alert_id)

    p.lookup.side_effect = lookup
    first = asyncio.create_task(p.module.prepare_bd_alert(alert_id))
    await entered.wait()
    second = asyncio.create_task(join())
    await joined.wait()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert not first.done()
    release.set()
    assert await first
    assert p.lookup.await_count == 1
    assert (await p.read(alert_id)).preparation is None


async def test_textual_edit_during_pricing_removes_links_of_the_previous_work(preparation):
    """Un jeton neuf ne suffit pas si les livres déjà liés à l'ancienne identité restent."""
    from app.db.models.alert_book import AlertBook
    from app.db.repositories.alert_repo import AlertRepository

    p = preparation
    alert_id = await p.create()
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(title, **_kwargs):
        if title == "Série synthétique":
            return CatalogueVerdict(
                consulted=("BNF",),
                notices=(BookMetadata(title="Ancien tome", isbn13="9782369904151"),),
            )
        return CatalogueVerdict(consulted=("BNF",))

    async def prices(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return MarketOffers()

    p.lookup.side_effect = lookup
    p.prices.side_effect = prices
    task = asyncio.create_task(p.module.prepare_bd_alert(alert_id))
    await entered.wait()
    async with p.db.maker() as session:
        repo = AlertRepository(session)
        alert = await repo.get_by_id(alert_id)
        await repo.lock_for_edit(alert)
        alert.name = "Nouvelle série"
        await repo.invalidate_preparation(alert, author_hint=None)
        await session.commit()
    release.set()
    assert await task
    async with p.db.maker() as session:
        assert (await session.execute(select(AlertBook))).scalars().all() == []


async def test_initial_preparation_deadline_blocks_without_activating(preparation, monkeypatch):
    original_timeout = asyncio.timeout
    scopes = []

    def timeout(seconds):
        scope = original_timeout(None)
        scopes.append((seconds, scope))
        return scope

    async def delayed_lookup(*_args, **_kwargs):
        scopes[0][1].reschedule(asyncio.get_running_loop().time())
        await asyncio.Event().wait()

    monkeypatch.setattr(preparation.module.asyncio, "timeout", timeout)
    preparation.lookup.side_effect = delayed_lookup
    alert_id = await preparation.create()
    assert not await preparation.module.prepare_bd_alert(alert_id)
    assert scopes[0][0] == 45.0
    current = await preparation.read(alert_id)
    assert current.status == AlertStatus.PAUSED
    assert current.preparation["state"] == "blocked"
