"""Modèle d'annonce ingérée depuis une marketplace."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db.base import Base, UUIDPrimaryKeyMixin, utc_now

if TYPE_CHECKING:
    from app.db.models.listing_match import ListingMatch


class Listing(Base, UUIDPrimaryKeyMixin):
    """Annonce ingérée depuis une source autorisée."""

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source_name", "external_id", name="uq_listings_source_external_id"),
    )

    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(500), index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_slug: Mapped[str] = mapped_column(String(50), index=True, nullable=False)

    # Prix en centimes
    item_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_protection_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_shipping_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_total_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    condition: Mapped[str | None] = mapped_column(String(100), nullable=True)
    seller_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seller_rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)

    # Photos stockées sous forme de liste d'URLs
    photos: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        default=list,
        nullable=False,
    )

    isbn13: Mapped[str | None] = mapped_column(String(13), index=True, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
        nullable=False,
    )

    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    )

    # Relations
    matches: Mapped[list["ListingMatch"]] = relationship(
        "ListingMatch",
        back_populates="listing",
        cascade="all, delete-orphan",
    )
