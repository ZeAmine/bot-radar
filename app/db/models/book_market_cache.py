"""Modèle de cache consolidé du meilleur prix de marché par livre."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

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
    # Projection dérivée uniquement des observations datées conservées.
    annual_low_cents: Mapped[int | None] = mapped_column(Integer)
    annual_low_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    annual_low_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    annual_low_provider: Mapped[str | None] = mapped_column(String(100))
    annual_first_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    annual_last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    annual_count: Mapped[int | None] = mapped_column(Integer)
    annual_computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    annual_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    last_successful_collection_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    historical_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collection_status: Mapped[str | None] = mapped_column(String(20))
    failed_provider_names: Mapped[list[str] | None] = mapped_column(JSON)
    current_offers: Mapped[list[dict[str, object]] | None] = mapped_column(JSON)

    book: Mapped["Book"] = relationship("Book", back_populates="market_cache")
