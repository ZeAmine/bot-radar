"""Modèle de catégorie pour les alertes."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin, utc_now

if TYPE_CHECKING:
    from app.db.models.alert import Alert


class Category(Base, UUIDPrimaryKeyMixin):
    """Catégorie d'articles (BD & Comics, Vêtements, Tech)."""

    __tablename__ = "categories"

    slug: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    emoji: Mapped[str] = mapped_column(String(10), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    # Relations
    alerts: Mapped[list["Alert"]] = relationship(
        "Alert",
        back_populates="category",
    )
