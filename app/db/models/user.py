"""Modèle utilisateur du bot Telegram."""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.alert import Alert
    from app.db.models.bot_event import BotEvent


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Utilisateur Telegram autorisé à utiliser Radar Bot."""

    __tablename__ = "users"

    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        index=True,
        nullable=False,
    )
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relations
    alerts: Mapped[list["Alert"]] = relationship(
        "Alert",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    events: Mapped[list["BotEvent"]] = relationship(
        "BotEvent",
        back_populates="user",
    )
