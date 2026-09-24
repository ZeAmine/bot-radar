"""Schémas Pydantic pour le sous-système IA (DeepSeek-V4.1-Flash)."""

from pydantic import BaseModel, Field


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


class NegotiationResult(BaseModel):
    """Message d'offre et proposition de prix pour la négociation."""

    suggested_offer_cents: int = Field(description="Montant de l'offre en centimes")
    suggested_offer_euros: float = Field(description="Montant en euros")
    message: str = Field(description="Message prêt à être copié-collé sur Vinted")
