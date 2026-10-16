"""La cotation planifiée résout les ouvrages manquants sans reprendre la surveillance."""

from time import monotonic

from sqlalchemy import select

from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.category import Category
from app.db.models.user import User
from app.domain.enums import AlertStatus, MatchConfidence
from app.services.book_catalog_service import BookCatalogService
from app.tasks import refresh_book_prices as refresh
from tests.database import postgres_replayed_database


async def test_scheduler_resolves_a_paused_unlinked_book_without_activating_it(monkeypatch):
    async with postgres_replayed_database() as db:
        category = Category(slug="bd_comics", label="Livres", emoji="📚")
        user = User(telegram_user_id=17, is_allowed=True)
        db.session.add_all([category, user])
        await db.session.flush()
        alert = Alert(user_id=user.id, category_id=category.id, name="Livre",
                      isbn="9782756080642", status=AlertStatus.PAUSED)
        db.session.add(alert)
        await db.session.commit()
        monkeypatch.setattr(refresh, "AsyncSessionLocal", db.maker)

        async def identify(self, *, isbn13, title):
            return await self.add_book(title=title, isbn13=isbn13), MatchConfidence.HIGH

        monkeypatch.setattr(BookCatalogService, "identify_book", identify)
        await refresh.resolve_scheduled_catalogues(incremental=False, deadline=monotonic()+60)
        async with db.maker() as reader:
            current = await reader.get(Alert, alert.id)
            assert current.status == AlertStatus.PAUSED
            assert current.catalogue_refresh_attempted_at is not None
            assert await reader.scalar(select(AlertBook).where(AlertBook.alert_id == alert.id))


async def test_scheduled_series_resolution_leaves_pricing_to_bounded_waves(monkeypatch):
    from app.services.book_metadata_service import (
        BookMetadata,
        BookMetadataService,
        CatalogueVerdict,
    )
    from app.use_cases import link_bd_catalog as catalog

    async with postgres_replayed_database() as db:
        category = Category(slug="bd_comics", label="Livres", emoji="📚")
        user = User(telegram_user_id=18, is_allowed=True)
        db.session.add_all([category, user])
        await db.session.flush()
        alert = Alert(user_id=user.id, category_id=category.id, name="Série",
                      status=AlertStatus.PAUSED)
        db.session.add(alert)
        await db.session.commit()
        monkeypatch.setattr(refresh, "AsyncSessionLocal", db.maker)
        monkeypatch.setattr("app.db.session.AsyncSessionLocal", db.maker)

        async def discover(*_args, **_kwargs):
            return CatalogueVerdict(notices=(BookMetadata(title="Série Tome 1",
                                                         isbn13="9782756080642"),))

        scheduled_requests = []

        def no_unbounded_pricing(book_id, **_kwargs):
            scheduled_requests.append(book_id)

        monkeypatch.setattr(BookMetadataService, "lookup_series_notices", discover)
        monkeypatch.setattr(catalog, "schedule_book_pricing", no_unbounded_pricing)
        await refresh.resolve_scheduled_catalogues(incremental=False, deadline=monotonic()+60)
        async with db.maker() as session:
            assert await session.scalar(select(AlertBook).where(AlertBook.alert_id == alert.id))
        assert scheduled_requests == []
