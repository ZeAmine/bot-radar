"""Journal des changements de réglages d'une alerte, une ligne par champ."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, UUIDPrimaryKeyMixin, utc_now


class AlertSettingChange(Base, UUIDPrimaryKeyMixin):
    """Ancienne et nouvelle valeur d'un réglage modifié, avec son origine."""

    __tablename__ = "alert_setting_changes"
    __table_args__ = (
        Index("ix_alert_setting_changes_alert_id_changed_at", "alert_id", "changed_at"),
    )

    alert_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("alerts.id", ondelete="CASCADE"),
        nullable=False,
    )
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[Any | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
        nullable=True,
    )
    new_value: Mapped[Any | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
        nullable=True,
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
    origin: Mapped[str] = mapped_column(String(200), nullable=False)
