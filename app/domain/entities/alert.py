"""Entité représentant une alerte utilisateur."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.domain.enums import AlertStatus, CategorySlug


@dataclass(slots=True)
class PauseActionProgress:
    """Connaissance locale d'une invocation : un commit échoué ne prouve pas son absence."""

    effect: Literal["not_started", "not_applied", "unknown", "committed"] = "not_started"
    token: UUID | None = None
    user_id: UUID | None = None
    update_id: int = 0
    callback_id: str = ""
    result: str = ""
    current_status: str = ""
    stage: Literal[
        "dispatch", "lookup", "mutation", "commit", "confirmation", "card", "handler_returned"
    ] = "dispatch"


@dataclass(slots=True)
class AlertEntity:
    """Entité pure représentant une alerte de surveillance configurée par l'utilisateur."""

    id: UUID
    user_id: UUID
    category_slug: CategorySlug
    name: str
    include_keywords: list[str]
    exclude_keywords: list[str] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)
    max_item_price_cents: int | None = None
    accepted_conditions: list[str] | None = None
    vinted_search_url: str | None = None
    isbn: str | None = None
    status: AlertStatus = AlertStatus.ACTIVE
    telegram_chat_id: int | None = None
    ai_analysis_enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
