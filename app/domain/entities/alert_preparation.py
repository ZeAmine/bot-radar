"""Demande durable de préparation BD, distincte du statut de surveillance."""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

PreparationErrorCode = Literal["catalog_write", "pricing_write", "activation_write", "unexpected"]


class PreparationError(Exception):
    """Échec technique distingué d'une absence normale de catalogue ou de cote."""

    def __init__(self, code: PreparationErrorCode) -> None:
        super().__init__(code)
        self.code = code


class PreparationRequest(BaseModel):
    """L'auteur demandé est un indice de recherche, jamais une identité confirmée."""

    token: str = Field(default_factory=lambda: str(uuid.uuid4()))
    state: Literal["pending", "blocked"] = "pending"
    activation_requested: bool = True
    author_hint: str | None = None
    bibliographic_mode: Literal["one_shot", "series"] | None = None
    series_volumes: list[dict[str, Any]] = Field(default_factory=list)
    series_reference_publisher: str | None = None
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_attempt_at: datetime | None = None
    error_code: PreparationErrorCode | None = None

    def restart(
        self, *, activate: bool, author_hint: str | None, preserve_series_volumes: bool = False,
    ) -> "PreparationRequest":
        """Invalide le travail précédent et repart sur les critères actuels."""
        return PreparationRequest(
            activation_requested=activate, author_hint=author_hint,
            bibliographic_mode=self.bibliographic_mode,
            series_volumes=self.series_volumes if preserve_series_volumes else [],
            series_reference_publisher=(self.series_reference_publisher
                                        if preserve_series_volumes else None),
        )

    def stored(self) -> dict[str, Any]:
        """Projection JSON du seul contrat de préparation."""
        return self.model_dump(mode="json")

    def suspend(self) -> "PreparationRequest":
        """Retire l'intention et invalide le travail, en conservant contexte et erreur."""
        return self.model_copy(update={"token": str(uuid.uuid4()), "activation_requested": False})


def preparation_request(alert: Any) -> PreparationRequest | None:
    """NULL et les anciens doubles sans métadonnées n'ont aucune demande à reprendre."""
    value = getattr(alert, "preparation", None)
    return PreparationRequest.model_validate(value) if isinstance(value, dict) else None
