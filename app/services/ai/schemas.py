"""Schémas Pydantic pour le sous-système IA (DeepSeek-V4.1-Flash)."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _LoosePayload(BaseModel):
    """JSON LLM : champs absents prennent le défaut, le surplus est ignoré."""

    model_config = ConfigDict(extra="ignore")


class NaturalAddResult(BaseModel):
    """Résultat du parsing en langage naturel d'une commande /add."""

    name: str = Field(description="Nom ou titre principal de l'alerte")
    category_slug: str = Field(
        default="bd_comics",
        description="Slug de catégorie: bd_comics, tech, gaming, clothes",
    )
    isbn13: str | None = Field(
        default=None, description="ISBN-13 officiel si livre unique / tome précis"
    )
    is_series: bool = Field(default=False, description="True s'il s'agit d'une série multi-tomes")
    author: str | None = Field(default=None, description="Auteur identifié")
    publisher: str | None = Field(default=None, description="Éditeur ou collection identifié")
    include_keywords: list[str] = Field(default_factory=list, description="Mots-clés obligatoires")
    exclude_keywords: list[str] = Field(default_factory=list, description="Mots-clés à exclure")
    max_price_cents: int | None = Field(default=None, description="Prix maximum en centimes")
    conditions: list[str] | None = Field(default=None, description="États acceptés")
    explanation: str | None = Field(default=None, description="Explication courte du découpage")


class ProfitScoringResult(BaseModel):
    """Scoring de rentabilité et indice pépite."""

    purchase_cost_cents: int | None = Field(
        default=None, description="Prix d'achat total estimé en centimes"
    )
    estimated_resale_cents: int | None = Field(default=None, description="Revente estimée")
    net_profit_cents: int | None = Field(default=None, description="Bénéfice net estimé")
    pepite_score: float | None = Field(default=None, description="Indice pépite de 1.0 à 10.0")
    verdict: str | None = Field(default=None, description="Conseil d'achat rapide")


class NaturalAddPayload(_LoosePayload):
    """Réponse brute de /add avant nettoyage."""

    category_slug: str = "bd_comics"
    conditions: list[Any] | None = None
    name: str | None = None
    include_keywords: list[Any] | None = None
    exclude_keywords: list[Any] | None = None
    is_series: bool = False
    isbn13: str | None = None
    author: str | None = None
    publisher: str | None = None
    max_price_cents: int | None = None
    explanation: str | None = None


class ProfitScoringPayload(_LoosePayload):
    """Portion JSON du scoring de rentabilité."""

    estimated_resale_cents: int | None = None
    net_profit_cents: int | None = None
    pepite_score: float | None = None
    verdict: str | None = None


class NegotiationPayload(_LoosePayload):
    """Portion JSON du coach de négociation."""

    suggested_offer_cents: int | None = None
    message: str | None = None


class KeywordVariantsPayload(_LoosePayload):
    """Variantes et exclusions suggérées."""

    variants: list[Any] | None = None
    exclusions: list[Any] | None = None


class AutoTuningPayload(_LoosePayload):
    """Exclusions suggérées après des faux positifs."""

    suggested_exclusions: list[Any] | None = None
    explanation: str | None = None


class NegotiationResult(BaseModel):
    """Message d'offre et proposition de prix pour la négociation."""

    suggested_offer_cents: int = Field(description="Montant de l'offre en centimes")
    suggested_offer_euros: float = Field(description="Montant en euros")
    message: str = Field(description="Message prêt à être copié-collé sur Vinted")
