"""Schémas Pydantic pour l'ingestion d'annonces."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ListingIngestPayload(BaseModel):
    """Payload d'ingestion sécurisé pour une annonce reçue."""

    model_config = ConfigDict(extra="ignore")

    source_name: str = Field(..., min_length=1, max_length=100)
    external_id: str = Field(..., min_length=1, max_length=255)
    url: str = Field(...)
    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = Field(default=None)
    category_slug: str = Field(..., min_length=1, max_length=50)

    item_price_cents: int = Field(..., ge=0)
    estimated_protection_cents: int | None = Field(default=None, ge=0)
    estimated_shipping_cents: int | None = Field(default=None, ge=0)

    condition: str | None = Field(default=None, max_length=100)
    seller_name: str | None = Field(default=None, max_length=255)
    seller_rating: Decimal | None = Field(default=None, ge=0, le=5)

    photos: list[str] = Field(default_factory=list, max_length=10)
    isbn13: str | None = Field(default=None, max_length=13)
    published_at: datetime | None = Field(default=None)

    @field_validator("category_slug")
    @classmethod
    def validate_category_slug(cls, v: str) -> str:
        cleaned = v.strip().lower().replace("-", "_")
        if cleaned in ("vetements", "vetement"):
            return "clothes"
        return cleaned

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned.lower().startswith("https://"):
            raise ValueError("L'URL de l'annonce doit être une URL sécurisée HTTPS.")
        return cleaned

    @field_validator("photos")
    @classmethod
    def validate_photos(cls, v: list[str]) -> list[str]:
        if len(v) > 10:
            raise ValueError("Le nombre de photos ne peut pas dépasser 10.")
        for p in v:
            if not p.strip().lower().startswith("https://"):
                raise ValueError("Chaque photo doit être une URL HTTPS valide.")
        return v


class ListingMatchItem(BaseModel):
    """Détail d'une alerte ayant correspondu à l'annonce."""

    alert_id: uuid.UUID
    alert_name: str
    notification_sent: bool


class ListingIngestResponse(BaseModel):
    """Réponse structurée retournée par le endpoint d'ingestion."""

    listing_id: uuid.UUID
    is_duplicate: bool
    estimated_total_cents: int | None
    matches: list[ListingMatchItem]
    needs_retry: bool = False
