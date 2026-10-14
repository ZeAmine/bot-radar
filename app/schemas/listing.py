"""Schémas Pydantic pour l'ingestion d'annonces."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    field_validator,
    model_validator,
)

from app.utils.isbn import normalize_listing_isbn

ListingIngestWarning = Literal["listing_isbn_invalid_ignored"]


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
    # Identifiant vendeur du catalogue Vinted, transporté pour la préparation du
    # checkout au clic sur « Acheter ». Optionnel : les annonces déjà en base ne
    # le portent pas, et la préparation le retrouve alors sur la page publique.
    seller_external_id: str | None = Field(default=None, max_length=64)

    photos: list[str] = Field(default_factory=list, max_length=1)
    isbn13: str | None = Field(default=None, max_length=13)
    published_at: datetime | None = Field(default=None)
    _isbn_invalid_ignored: bool = PrivateAttr(default=False)

    @model_validator(mode="wrap")
    @classmethod
    def normalize_seller_isbn(cls, data: Any, handler: ModelWrapValidatorHandler[Self]) -> Self:
        """Un code inutilisable n'empêche pas l'ingestion textuelle de l'annonce."""
        invalid = False
        if isinstance(data, dict) and isinstance(data.get("isbn13"), str):
            raw = data["isbn13"]
            code = normalize_listing_isbn(raw)
            invalid = code is None
            data = {**data, "isbn13": code}
        result = handler(data)
        if isinstance(data, dict):
            result._isbn_invalid_ignored = invalid
        return result

    @property
    def isbn_invalid_ignored(self) -> bool:
        """Diagnostic de validation, indépendant du JSON brut conservé par l'ingestion."""
        return self._isbn_invalid_ignored

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
        """Seule la photo de couverture de l'alerte Telegram est conservée."""
        if len(v) > 1:
            raise ValueError("Une seule photo (couverture) est acceptée.")
        for p in v:
            if not p.strip().lower().startswith("https://"):
                raise ValueError("La photo doit être une URL HTTPS valide.")
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
    warnings: list[ListingIngestWarning] = Field(default_factory=list)
