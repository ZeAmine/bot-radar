"""Modèle d'association entre une alerte et un livre de référence."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import MatchConfidence
from app.db.base import Base, utc_now

if TYPE_CHECKING:
    from app.db.models.alert import Alert
    from app.db.models.book import Book


class AlertBook(Base):
    """Liaison entre une alerte BD et un livre du catalogue interne."""

    __tablename__ = "alert_books"

    alert_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("alerts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    book_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("books.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    match_confidence: Mapped[MatchConfidence] = mapped_column(
        Enum(MatchConfidence, native_enum=False, length=20),
        default=MatchConfidence.HIGH,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    # Relations
    alert: Mapped["Alert"] = relationship("Alert", back_populates="alert_books")
    book: Mapped["Book"] = relationship("Book", back_populates="alert_books")
