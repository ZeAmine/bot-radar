"""Modèle d'instantané de prix de marché collecté auprès des fournisseurs."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.constants import MatchConfidence
from app.db.base import Base, UUIDPrimaryKeyMixin, utc_now

if TYPE_CHECKING:
    from app.db.models.book import Book


class MarketPriceSnapshot(Base, UUIDPrimaryKeyMixin):
    """Offre unitaire relevée sur le marché d'occasion pour un livre donné."""

    __tablename__ = "market_price_snapshots"

    book_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("books.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    provider_name: Mapped[str] = mapped_column(String(100), index=True, nullable=False)

    item_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    shipping_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivered_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)

    item_condition: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
        nullable=False,
    )
    is_comparable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    confidence: Mapped[MatchConfidence] = mapped_column(
        Enum(MatchConfidence, native_enum=False, length=20),
        default=MatchConfidence.HIGH,
        nullable=False,
    )
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    )

    # Relations
    book: Mapped["Book"] = relationship("Book", back_populates="snapshots")
