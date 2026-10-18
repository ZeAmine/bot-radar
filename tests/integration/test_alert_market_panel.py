"""Le marché d'une alerte se consulte sans aucune annonce reçue."""

from unittest.mock import AsyncMock

from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.category import Category
from app.db.models.user import User
from app.use_cases.view_market_details import ViewMarketDetailsUseCase
from tests.database import postgres_replayed_database


async def test_alert_market_starts_at_tome_one_without_listing_or_network(monkeypatch):
    async with postgres_replayed_database() as database:
        s = database.session
        user = User(telegram_user_id=4242, is_allowed=True)
        category = Category(slug="bd_comics", label="BD", emoji="📚", is_active=True)
        s.add_all([user, category])
        await s.flush()
        alert = Alert(
            user_id=user.id,
            category_id=category.id,
            name="Le Château des animaux",
            include_keywords=["château"],
            status=AlertStatus.ACTIVE,
        )
        s.add(alert)
        await s.flush()
        books = [
            Book(title=f"Le Château des animaux Tome {n}", normalized_title=f"chateau {n}")
            for n in (2, 1)
        ]
        s.add_all(books)
        await s.flush()
        s.add_all([AlertBook(alert_id=alert.id, book_id=b.id) for b in books])
        await s.commit()
        pricing = AsyncMock(side_effect=AssertionError("Pas de réseau à l'ouverture"))
        monkeypatch.setattr("app.use_cases.view_market_details.price_book_now", pricing)
        panel = await ViewMarketDetailsUseCase(s).execute_for_alert(alert.id, user.id)
        assert panel is not None
        assert panel.listing is None
        assert panel.book.title.endswith("Tome 1")
        assert [b.title[-1] for b in panel.books_in_series] == ["1", "2"]
        assert await ViewMarketDetailsUseCase(s).execute_for_alert(alert.id, books[0].id) is None


async def test_creation_reloads_a_cache_written_in_another_session(monkeypatch):
    from app.bot.handlers.alerts_create import save_alert
    from app.core.constants import ComparisonStatus
    from app.db.models.book_market_cache import BookMarketCache
    from tests.add_fakes import DraftState
    from tests.screens import confirm_press

    async with postgres_replayed_database() as database:
        session = database.session
        user = User(telegram_user_id=1414, is_allowed=True)
        category = Category(slug="bd_comics", label="BD", emoji="📚", is_active=True)
        book = Book(title="Livre", normalized_title="livre", isbn13="9782369904151")
        session.add_all([user, category, book])
        await session.flush()
        cache = BookMarketCache(
            book_id=book.id,
            lowest_delivered_price_cents=1000,
            comparison_status=ComparisonStatus.AVAILABLE,
        )
        session.add(cache)
        await session.commit()

        async def price(book_id, *, reason):
            assert reason == "alert_created"
            async with database.maker() as other:
                from app.db.repositories.market_price_repo import MarketPriceRepository

                changed = await MarketPriceRepository(other).get_cache_by_book_id(book_id)
                from datetime import UTC, datetime, timedelta
                changed.lowest_delivered_price_cents = 1500
                changed.checked_at = datetime.now(UTC)
                changed.expires_at = changed.checked_at + timedelta(hours=24)
                await other.commit()
                return changed

        from app.use_cases import prepare_bd_alert as preparation

        monkeypatch.setattr(preparation, "price_book_now", price)
        monkeypatch.setattr(preparation, "AsyncSessionLocal", database.maker)
        monkeypatch.setattr(preparation, "preparation_runner", preparation.PreparationRunner())
        state = DraftState()
        # L'identité FSM est requise par le guard de création.
        state.storage = object()
        state.key = ("synthetic", 1414)
        state.data = {
            "name": "Livre",
            "category_slug": "bd_comics",
            "isbn": book.isbn13,
            "include_keywords": ["livre"],
        }
        callback, message = confirm_press()
        await save_alert(callback, state, user, session)
        from sqlalchemy import select

        created = (await session.execute(select(Alert))).scalar_one()
        assert created.status == AlertStatus.ACTIVE
        assert "<b>Dernière offre connue :</b> 15,00 €" in message.edit_text.call_args.args[0]
