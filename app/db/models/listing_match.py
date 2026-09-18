"""Modèle de correspondance entre une alerte et une annonce."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin, utc_now

if TYPE_CHECKING:
    from app.db.models.alert import Alert
    from app.db.models.listing import Listing


class ListingMatch(Base, UUIDPrimaryKeyMixin):
    """Association et historique de notification entre une alerte et une annonce."""

    __tablename__ = "listing_matches"
    __table_args__ = (
        UniqueConstraint("alert_id", "listing_id", name="uq_listing_matches_alert_listing"),
    )

    alert_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("alerts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    listing_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("listings.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    keyword_match_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    condition_matches: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    price_matches: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    # Relations
    alert: Mapped["Alert"] = relationship("Alert", back_populates="matches")
    listing: Mapped["Listing"] = relationship("Listing", back_populates="matches")
