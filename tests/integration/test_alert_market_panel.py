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
