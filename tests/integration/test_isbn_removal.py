"""Retrait réel d'ISBN : nettoyage local, livre partagé et résultats devenus périmés."""

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.bot.handlers import alerts_manage as edit
from app.bot.handlers import autotune
from app.bot.handlers.autotune import (
    get_pending_variants,
    pop_pending_variants,
    save_pending_variants,
)
from app.core import tasks
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository, BibliographicSnapshot
from app.db.repositories.book_repo import BookRepository
from app.domain.entities.alert_preparation import PreparationRequest
from app.domain.enums import AlertStatus, ComparisonStatus, MatchConfidence
from app.schemas.listing import ListingIngestPayload
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome
from app.services.book_metadata_service import BookMetadata, BookMetadataService, CatalogueVerdict
from app.use_cases import ingest_listing as ingestion
from app.use_cases import link_bd_catalog as catalog_linking
from tests.add_fakes import DraftState
from tests.database import postgres_replayed_database
from tests.telegram_fakes import acknowledged_delivery

OLD = "9782369903086"
NEW = "9782369904151"


@pytest.fixture
async def journey(monkeypatch):
    async with postgres_replayed_database() as db:
        db.maker.configure(autoflush=False)
        monkeypatch.setattr(tasks, "_background_tasks", set())
        monkeypatch.setattr(tasks, "_cleanup_tasks", set())
        monkeypatch.setattr("app.db.session.AsyncSessionLocal", db.maker)
        monkeypatch.setattr(edit, "AsyncSessionLocal", db.maker)
        monkeypatch.setattr(
            edit.keyword_variants_module,
            "generate_variants_and_exclusions",
            AsyncMock(return_value=KeywordVariantsOutcome(variants=[], exclusions=[])),
        )
        category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        user = User(telegram_user_id=601, is_allowed=True)
        db.session.add_all([category, user])
        await db.session.flush()
        book = Book(title="Cible BD", normalized_title="cible bd", isbn13=OLD)
        book.market_cache = BookMarketCache(
            lowest_delivered_price_cents=1800,
            historical_low_1y_cents=1600,
            confidence=MatchConfidence.HIGH,
            comparison_status=ComparisonStatus.AVAILABLE,
            checked_at=datetime.now(UTC),
        )
        db.session.add(book)
        await db.session.flush()
        alert = Alert(
            user_id=user.id,
            category_id=category.id,
            name="Cible BD",
            isbn=OLD,
            include_keywords=["cible", OLD, "manuel"],
            variants=["variante manuelle", OLD],
            exclude_keywords=["exclusion manuelle", OLD],
            status=AlertStatus.ACTIVE,
            series_total_cents=9000,
            book_search_exhausted_at=datetime.now(UTC),
        )
        other = Alert(
            user_id=user.id,
            category_id=category.id,
            name="Autre alerte",
            isbn=OLD,
            include_keywords=["autre"],
            status=AlertStatus.ACTIVE,
        )
        db.session.add_all([alert, other])
        await db.session.flush()
        db.session.add_all(
            [
                AlertBook(alert_id=alert.id, book_id=book.id),
                AlertBook(alert_id=other.id, book_id=book.id),
            ]
        )
        await db.session.commit()
        save_pending_variants(alert.id, ["ancienne suggestion"])
        prepare = AsyncMock(return_value=False)
        monkeypatch.setattr(edit, "prepare_bd_alert", prepare)
        monkeypatch.setattr(edit, "price_book_now", AsyncMock())
        monkeypatch.setattr(
            edit,
            "_lookup_isbn_notice",
            AsyncMock(return_value=BookMetadata(title="Cible BD", isbn13=NEW)),
        )
        monkeypatch.setattr(ingestion, "schedule_book_pricing", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            catalog_linking, "schedule_book_pricing", lambda *_args, **_kwargs: None
        )

        async def change(value="-", fail_commit=False):
            state = DraftState()
            state.data = {"edit_alert_id": str(alert.id), "edit_field": "isbn"}
            message = AsyncMock()
            message.text = value
            async with db.maker() as session:
                if fail_commit:

                    async def fail():
                        raise RuntimeError("Échec synthétique du commit")

                    monkeypatch.setattr(session, "commit", fail)
                await edit.save_edited_field_value(message, state, user, session)
            return message

        async def read():
            async with db.maker() as session:
                current = await AlertRepository(session).get_by_id(alert.id)
                await session.refresh(current, ["alert_books"])
                return current

        yield SimpleNamespace(
            db=db,
            alert=alert,
            other=other,
            book=book,
            user=user,
            change=change,
            read=read,
            prepare=prepare,
        )
        pop_pending_variants(alert.id)
        await tasks.wait_all_background_tasks(timeout=0)


@pytest.mark.parametrize("value", ["-", "/skip", "aucun", "aucune", "clear", "supprimer"])
async def test_remove_keeps_shared_book_and_manual_criteria(journey, value):
    message = await journey.change(value)
    current = await journey.read()
    assert current.isbn is None
    assert current.alert_books == []
    assert current.series_total_cents is None
    assert current.book_search_exhausted_at is None
    assert current.include_keywords == ["cible", "manuel"]
    assert current.variants == ["variante manuelle"]
    assert current.exclude_keywords == ["exclusion manuelle"]
    assert current.name == "Cible BD"
    assert current.status == AlertStatus.ACTIVE
    assert current.preparation is None
    assert get_pending_variants(current.id) == []
    journey.prepare.assert_not_awaited()
    assert message.answer.await_count >= 2
    async with journey.db.maker() as session:
        assert len((await session.execute(select(Book))).scalars().all()) == 1
        assert len((await session.execute(select(BookMarketCache))).scalars().all()) == 1
        links = (await session.execute(select(AlertBook))).scalars().all()
        assert len(links) == 1
        assert links[0].alert_id == journey.other.id


async def test_repeat_does_not_remove_a_fresh_title_discovery(journey):
    await journey.change()
    async with journey.db.maker() as session:
        session.add(AlertBook(alert_id=journey.alert.id, book_id=journey.book.id))
        await session.commit()
    save_pending_variants(journey.alert.id, ["suggestion actuelle"])
    await journey.change()
    assert len((await journey.read()).alert_books) == 1
    assert get_pending_variants(journey.alert.id) == ["suggestion actuelle"]


async def test_only_exact_old_isbn_terms_are_removed(journey):
    async with journey.db.maker() as session:
        alert = await AlertRepository(session).get_by_id(journey.alert.id)
        alert.include_keywords = ["cible", OLD, NEW, f"isbn {OLD}"]
        alert.variants = [OLD, f"variante {OLD}"]
        alert.exclude_keywords = [OLD, NEW]
        await session.commit()
    await journey.change()
    current = await journey.read()
    assert current.include_keywords == ["cible", NEW, f"isbn {OLD}"]
    assert current.variants == [f"variante {OLD}"]
    assert current.exclude_keywords == [NEW]


async def test_pending_request_keeps_author_and_activation_intention(journey):
    request = PreparationRequest(
        author_hint="Auteur explicitement demandé", activation_requested=False
    )
    async with journey.db.maker() as session:
        alert = await AlertRepository(session).get_by_id(journey.alert.id)
        alert.status = AlertStatus.PAUSED
        alert.preparation = request.stored()
        await session.commit()
    await journey.change()
    current = await journey.read()
    assert current.alert_books == []
    assert current.preparation["token"] != request.token
    assert current.preparation["author_hint"] == request.author_hint
    assert current.preparation["activation_requested"] is False
    assert current.status == AlertStatus.PAUSED


async def test_commit_failure_preserves_identity_links_and_suggestions(journey):
    before = await journey.read()
    with pytest.raises(RuntimeError, match="Échec synthétique"):
        await journey.change(fail_commit=True)
    after = await journey.read()
    assert (after.isbn, after.series_total_cents) == (before.isbn, before.series_total_cents)
    assert len(after.alert_books) == 1
    assert after.book_search_exhausted_at == before.book_search_exhausted_at
    assert after.include_keywords == before.include_keywords
    assert get_pending_variants(after.id) == ["ancienne suggestion"]


async def test_replacement_commit_clears_derived_data_before_pricing(journey, monkeypatch):
    async def price(*_args, **_kwargs):
        current = await journey.read()
        assert current.isbn == NEW
        assert current.series_total_cents is None
        assert current.book_search_exhausted_at is None
        async with journey.db.maker() as session:
            linked = (
                (
                    await session.execute(
                        select(Book.isbn13).join(AlertBook).where(AlertBook.alert_id == current.id)
                    )
                )
                .scalars()
                .all()
            )
            assert linked == [NEW]
        assert current.variants == ["variante manuelle"]
        assert current.exclude_keywords == ["exclusion manuelle"]

    monkeypatch.setattr(edit, "price_book_now", price)
    await journey.change(NEW)


@pytest.mark.parametrize("aba", [False, True])
async def test_late_title_result_is_abandoned_after_isbn_change(journey, monkeypatch, aba):
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return CatalogueVerdict(
            consulted=("BNF",), notices=(BookMetadata(title="Cible BD", isbn13=OLD),)
        )

    monkeypatch.setattr(BookMetadataService, "lookup_by_title_and_author", lookup)
    task = asyncio.create_task(catalog_linking.link_books_from_bnf("Cible BD", journey.alert.id))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        await journey.change()
        if aba:
            monkeypatch.setattr(
                edit,
                "_lookup_isbn_notice",
                AsyncMock(return_value=BookMetadata(title="Cible BD", isbn13=OLD)),
            )
            await journey.change(OLD)
            # L'ancien résultat ne doit pas réécrire ce verdict nouveau.
            async with journey.db.maker() as session:
                alert = await AlertRepository(session).get_by_id(journey.alert.id)
                alert.book_search_exhausted_at = datetime.now(UTC)
                await session.commit()
    finally:
        release.set()
        result = await task
    assert result is False
    if not aba:
        assert (await journey.read()).alert_books == []
    else:
        assert (await journey.read()).book_search_exhausted_at is not None


async def test_new_title_discovery_after_removal_is_allowed(journey, monkeypatch):
    await journey.change()
    monkeypatch.setattr(
        BookMetadataService,
        "lookup_series_notices",
        AsyncMock(
            return_value=CatalogueVerdict(
                consulted=("BNF",), notices=(BookMetadata(title="Cible BD", isbn13=OLD),)
            )
        ),
    )
    assert await catalog_linking.link_books_from_bnf("Cible BD", journey.alert.id, series=True)
    assert len((await journey.read()).alert_books) == 1


async def test_new_ingestion_does_not_use_removed_reference(journey, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [journey.user.telegram_user_id])
    await journey.change()
    sender = AsyncMock(side_effect=acknowledged_delivery)
    # La confirmation n'est pas le sujet : aucun travail réseau détaché dans ce scénario.
    monkeypatch.setattr(ingestion, "safe_create_task", lambda coro, **_kwargs: coro.close())
    payload = ListingIngestPayload(
        source_name="synthetic",
        external_id="1",
        url="https://example.invalid/1",
        title="Cible BD manuel",
        category_slug="bd_comics",
        item_price_cents=100,
    )
    async with journey.db.maker() as session:
        result = await ingestion.ProcessListingUseCase(
            session, SimpleNamespace(id=123456789), sender=sender
        ).execute(payload, {})
    assert result.matches[0].notification_sent
    assert "16,00" not in sender.call_args.kwargs["market_comparison_line"]


@pytest.mark.parametrize("aba", [False, True])
async def test_historical_queued_isbn_result_cannot_write_again(journey, monkeypatch, aba):
    initial = await journey.read()
    snapshot = BibliographicSnapshot.capture(initial)
    await journey.change()
    if aba:
        monkeypatch.setattr(
            edit,
            "_lookup_isbn_notice",
            AsyncMock(return_value=BookMetadata(title="Cible BD", isbn13=OLD)),
        )
        await journey.change(OLD)
    async with journey.db.maker() as session:
        current = await AlertRepository(session).get_by_id(journey.alert.id)
        current.book_search_exhausted_at = datetime.now(UTC)
        await session.commit()
    before = await journey.read()
    await ingestion._link_bd_catalog_background(
        alert_id=initial.id,
        alert_name=initial.name,
        alert_isbn=initial.isbn,
        listing_title="Cible BD",
        listing_description=None,
        listing_isbn13=None,
        alert_snapshot=snapshot,
    )
    after = await journey.read()
    assert after.book_search_exhausted_at == before.book_search_exhausted_at
    assert len(after.alert_books) == len(before.alert_books)


async def test_keywords_in_flight_cannot_erase_manual_criteria_after_removal(journey, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return KeywordVariantsOutcome(variants=["ancien résultat"], exclusions=["ancienne réponse"])

    monkeypatch.setattr(edit.keyword_variants_module, "generate_variants_and_exclusions", generate)
    initial = await journey.read()
    task = asyncio.create_task(
        edit._refresh_alert_keywords(
            initial.id,
            expected_isbn=OLD,
            name=initial.name,
            category_slug="bd_comics",
            book_context=None,
            target=AsyncMock(),
            expected_revision=initial.updated_at,
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    try:
        await journey.change()
    finally:
        release.set()
        await task
    after = await journey.read()
    assert after.variants == ["variante manuelle"]
    assert after.exclude_keywords == ["exclusion manuelle"]


async def test_recap_uses_refreshed_relationship(journey, monkeypatch):
    observed = []
    original = edit.format_alert_card

    def render(alert, **kwargs):
        observed.append(list(alert.alert_books))
        return original(alert, **kwargs)

    monkeypatch.setattr(edit, "format_alert_card", render)
    await journey.change()
    assert observed == [[]]


async def test_expired_suggestion_cannot_be_applied_after_wait(journey, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = AlertRepository.get_by_id

    async def held(repo, alert_id, *args, **kwargs):
        if asyncio.current_task().get_name() == "synthetic-apply":
            entered.set()
            await release.wait()
        return await original(repo, alert_id, *args, **kwargs)

    monkeypatch.setattr(AlertRepository, "get_by_id", held)
    callback = AsyncMock()
    callback.data = f"var_apply:{journey.alert.id}"
    async with journey.db.maker() as session:
        task = asyncio.create_task(
            autotune.handle_var_apply(callback, journey.user, session), name="synthetic-apply"
        )
        await asyncio.wait_for(entered.wait(), 2)
        try:
            await journey.change()
        finally:
            release.set()
            await task
    assert (await journey.read()).variants == ["variante manuelle"]
    assert "expiré" in callback.answer.call_args.args[0]


@pytest.mark.parametrize("writer", ["ingest", "market"])
async def test_local_isbn_lookup_cannot_restore_link_after_removal(journey, monkeypatch, writer):
    from app.use_cases.view_market_details import ViewMarketDetailsUseCase

    entered, release = asyncio.Event(), asyncio.Event()
    original = BookRepository.get_by_isbn13

    async def held(repo, isbn):
        entered.set()
        await release.wait()
        return await original(repo, isbn)

    monkeypatch.setattr(BookRepository, "get_by_isbn13", held)
    async with journey.db.maker() as session:
        await BookRepository(session).clear_alert_books(journey.alert.id)
        await session.commit()
    async with journey.db.maker() as session:
        alert = await AlertRepository(session).get_by_id(journey.alert.id)
        await session.commit()
        if writer == "ingest":
            task = asyncio.create_task(
                ingestion.ProcessListingUseCase(session, AsyncMock())._comparison_from_catalog_isbn(
                    SimpleNamespace(isbn13=None, title="Cible BD", category_slug="bd_comics"), alert
                )
            )
        else:
            task = asyncio.create_task(
                ViewMarketDetailsUseCase(session).resolve_alert_books(alert.id)
            )
        await asyncio.wait_for(entered.wait(), 2)
        try:
            await journey.change()
        finally:
            release.set()
            await task
        await session.commit()
    assert (await journey.read()).alert_books == []


async def test_isbn_lookup_aba_with_a_distinct_request_is_rejected(journey, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(isbn):
        if isbn == NEW:
            entered.set()
            await release.wait()
        return BookMetadata(title="Cible BD", isbn13=isbn)

    monkeypatch.setattr(edit, "_lookup_isbn_notice", lookup)
    state = DraftState()
    state.data = {"edit_alert_id": str(journey.alert.id), "edit_field": "isbn"}
    message = AsyncMock()
    message.text = NEW
    async with journey.db.maker() as session:
        task = asyncio.create_task(
            edit.save_edited_field_value(message, state, journey.user, session)
        )
        await asyncio.wait_for(entered.wait(), 2)
        try:
            await journey.change()
            await journey.change(OLD)
        finally:
            release.set()
            await task
    assert (await journey.read()).isbn == OLD
    assert any("changé" in call.args[0] for call in message.answer.await_args_list)


@pytest.mark.parametrize("blocked", [False, True])
async def test_explicit_suggestions_guard_keeps_current_manual_lists(journey, blocked):
    request = PreparationRequest(
        author_hint="Auteur demandé",
        activation_requested=not blocked,
        state="blocked" if blocked else "pending",
    )
    async with journey.db.maker() as session:
        alert = await AlertRepository(session).get_by_id(journey.alert.id)
        alert.status = AlertStatus.PAUSED
        alert.preparation = request.stored()
        await session.commit()
    async with journey.db.maker() as first:
        repo = AlertRepository(first)
        old = await repo.get_by_id(journey.alert.id)
        snapshot = BibliographicSnapshot.capture(old)
        await first.commit()
        async with journey.db.maker() as editor:
            current = await AlertRepository(editor).get_by_id(journey.alert.id)
            current.variants = ["critère manuel actuel"]
            await editor.commit()
        guarded = await repo.lock_bibliographic_result(old.id, snapshot)
        assert guarded is not None
        assert guarded.variants == ["critère manuel actuel"]
        if blocked:
            assert (
                await repo.lock_bibliographic_result(old.id, snapshot, require_preparation=True)
                is None
            )


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="Verrou PostgreSQL réel requis")
async def test_result_waiting_for_edit_lock_cannot_restore_removed_link(journey, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return CatalogueVerdict(
            consulted=("BNF",), notices=(BookMetadata(title="Cible BD", isbn13=OLD),)
        )

    monkeypatch.setattr(BookMetadataService, "lookup_by_title_and_author", lookup)
    task = asyncio.create_task(catalog_linking.link_books_from_bnf("Cible BD", journey.alert.id))
    await asyncio.wait_for(entered.wait(), 2)
    async with journey.db.maker() as session:
        repo = AlertRepository(session)
        alert = await repo.get_by_id(journey.alert.id)
        assert await repo.lock_for_edit(alert)
        # Même écriture utilisée par le parcours de modification, avant son commit.
        alert.isbn = None
        await repo.reset_bibliographic_identity(alert)
        release.set()
        await session.commit()
    assert await task is False
    assert (await journey.read()).alert_books == []


async def test_new_pause_during_pricing_survives_an_active_paused_roundtrip(journey, monkeypatch):
    """Le statut revenu en pause ne réhabilite pas une activation ancienne."""
    from app.db.repositories.alert_repo import AlertRepository

    async def price(*_args, **_kwargs):
        async with journey.db.maker() as session:
            repo = AlertRepository(session)
            current = await repo.get_by_id(journey.alert.id)
            assert current.status == AlertStatus.PAUSED
            await repo.set_status(current, AlertStatus.ACTIVE)
            await repo.set_status(current, AlertStatus.PAUSED)
            await session.commit()

    monkeypatch.setattr(edit, "price_book_now", price)
    await journey.change(NEW)
    current = await journey.read()
    assert current.isbn == NEW
    assert current.status == AlertStatus.PAUSED
