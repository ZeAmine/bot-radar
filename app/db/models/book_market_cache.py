"""Modèle de cache consolidé du meilleur prix de marché par livre."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.book import Book


class BookMarketCache(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Cache consolidé de la meilleure offre d'occasion disponible pour un livre."""

    __tablename__ = "book_market_cache"

    book_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("books.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    lowest_delivered_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_provider_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    best_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[MatchConfidence | None] = mapped_column(
        Enum(MatchConfidence, native_enum=False, length=20),
        nullable=True,
    )
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=True,
    )
    comparison_status: Mapped[ComparisonStatus] = mapped_column(
        Enum(ComparisonStatus, native_enum=False, length=20),
        default=ComparisonStatus.UNAVAILABLE,
        nullable=False,
    )
    historical_low_1y_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    historical_low_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relations
    book: Mapped["Book"] = relationship("Book", back_populates="market_cache")
