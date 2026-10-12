"""Autorisation de pause et preuve atomique ; aucune file de demandes."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class PauseAction(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "pause_actions"

    # Références conservées 48 h même si l'alerte est supprimée entre-temps.
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    alert_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    intent: Mapped[str] = mapped_column(String(24), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invalidated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_version: Mapped[int | None] = mapped_column(Integer)
    update_id: Mapped[int | None] = mapped_column(BigInteger)
    callback_id: Mapped[str | None] = mapped_column(String(128))
