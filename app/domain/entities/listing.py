"""Entité représentant une annonce dans le domaine métier."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.domain.enums import CategorySlug


@dataclass(slots=True)
class ListingEntity:
    """Entité pure représentant une annonce issue d'une source (Vinted, etc.)."""

    id: UUID | None
    source_name: str
    external_id: str
    title: str
    normalized_title: str
    category_slug: CategorySlug
    item_price_cents: int
    shipping_price_cents: int | None = None
    total_price_cents: int | None = None
    buyer_protection_fee_cents: int | None = None
    condition: str | None = None
    description: str | None = None
    url: str = ""
    photo_url: str | None = None
    photos: list[str] = field(default_factory=list)
    isbn13: str | None = None
    seller_name: str | None = None
    created_at: datetime | None = None
