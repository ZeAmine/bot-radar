"""Modèle d'alerte utilisateur."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.constants import AlertStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.alert_book import AlertBook
    from app.db.models.category import Category
    from app.db.models.listing_match import ListingMatch
    from app.db.models.user import User


class Alert(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Alerte définie par l'utilisateur pour surveiller des annonces."""

    __tablename__ = "alerts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("categories.id"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    isbn: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    vinted_search_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Listes de mots-clés et variantes
    include_keywords: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        default=list,
        nullable=False,
    )
    exclude_keywords: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        default=list,
        nullable=False,
    )
    variants: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        default=list,
        nullable=False,
    )

    # Seuil de prix article seul (hors port et protection)
    max_item_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Total des prix de référence des tomes d'une série (NULL : aucune cote exploitable).
    # Réécrit à chaque écriture de cote par `persist_market_result` — jamais ailleurs.
    series_total_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # États acceptés (liste de chaînes ou null si tous acceptés)
    accepted_conditions: Mapped[list[str] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    )

    # Statut de l'alerte
    status: Mapped[AlertStatus] = mapped_column(
        Enum(AlertStatus, native_enum=False, length=20),
        default=AlertStatus.ACTIVE,
        index=True,
        nullable=False,
    )

    # Horodatage pour soft delete
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relations
    user: Mapped["User"] = relationship("User", back_populates="alerts")
    category: Mapped["Category"] = relationship("Category", back_populates="alerts")
    alert_books: Mapped[list["AlertBook"]] = relationship(
        "AlertBook",
        back_populates="alert",
        cascade="all, delete-orphan",
    )
    matches: Mapped[list["ListingMatch"]] = relationship(
        "ListingMatch",
        back_populates="alert",
        cascade="all, delete-orphan",
    )
