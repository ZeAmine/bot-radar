"""Entité et schéma de sortie structurée pour l'analyse IA."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RealConditionAssessment(StrEnum):
    """Estimation de l'état réel déclarée dans l'annonce."""

    EXCELLENT = "excellent"
    VERY_GOOD = "very_good"
    GOOD = "good"
    ACCEPTABLE = "acceptable"
    POOR_OR_DEFECTIVE = "poor_or_defective"


class ListingAnalysisResult(BaseModel):
    """Résultat structuré de l'analyse d'annonce par le LLM."""

    model_config = ConfigDict(frozen=True)

    real_condition: RealConditionAssessment = Field(
        description="État physique réel déduit de la description de l'annonce"
    )
    is_lot_incomplete: bool = Field(
        default=False,
        description="True si tomes, pièces ou accessoires manquants",
    )
    is_scam_or_trap: bool = Field(
        default=False,
        description="True si boîte vide, contrefaçon ou anomalie suspecte",
    )
    risk_score: int = Field(
        ge=0,
        le=100,
        description="Score de risque d'achat (0 = aucun risque, 100 = arnaque avérée)",
    )
    analysis_summary: str = Field(
        max_length=250,
        description="Résumé concis en 1 phrase pour la notification Telegram",
    )
