"""Modèle de livre pour le catalogue interne BD & Comics."""

from typing import TYPE_CHECKING

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.alert_book import AlertBook
    from app.db.models.book_market_cache import BookMarketCache
    from app.db.models.market_price_snapshot import MarketPriceSnapshot


class Book(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Livre de référence dans le catalogue BD & Comics pour la comparaison de prix."""

    __tablename__ = "books"

    isbn13: Mapped[str | None] = mapped_column(
        String(13),
        unique=True,
        index=True,
        nullable=True,
    )
    isbn10: Mapped[str | None] = mapped_column(String(10), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(255), nullable=True)
    publication_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    edition: Mapped[str | None] = mapped_column(String(255), nullable=True)
    format: Mapped[str | None] = mapped_column(String(100), nullable=True)
    language: Mapped[str] = mapped_column(String(10), default="fr", nullable=False)
    cover_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relations
    alert_books: Mapped[list["AlertBook"]] = relationship(
        "AlertBook",
        back_populates="book",
        cascade="all, delete-orphan",
    )
    snapshots: Mapped[list["MarketPriceSnapshot"]] = relationship(
        "MarketPriceSnapshot",
        back_populates="book",
        cascade="all, delete-orphan",
    )
    market_cache: Mapped["BookMarketCache | None"] = relationship(
        "BookMarketCache",
        back_populates="book",
        uselist=False,
        cascade="all, delete-orphan",
    )
