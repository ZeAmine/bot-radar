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


class PriceGlitchResult(BaseModel):
    """Résultat de l'analyse d'anomalie de prix (Price Glitch)."""

    is_glitch: bool = Field(default=False, description="True si le prix est anormalement bas")
    discount_percent: int | None = Field(default=None, description="Pourcentage sous la cote")
    estimated_market_value_cents: int | None = Field(default=None, description="Valeur estimée")
    reason: str | None = Field(default=None, description="Explication concise de l'anomalie")


class HiddenConditionResult(BaseModel):
    """Résultat de l'analyse des défauts cachés dans la description."""

    has_hidden_defects: bool = Field(default=False, description="True si défauts détectés")
    declared_condition: str | None = Field(default=None, description="État déclaré par le vendeur")
    real_condition_summary: str | None = Field(default=None, description="Synthèse de l'état réel")
    defects_found: list[str] = Field(default_factory=list, description="Liste des défauts textuels")
    severity: str = Field(default="faible", description="Gravité: faible, moyen, critique")


class BundleDecompositionResult(BaseModel):
    """Décomposition d'un lot et calcul du prix unitaire."""

    is_bundle: bool = Field(default=False, description="True s'il s'agit d'un ensemble / lot")
    items_detected: list[str] = Field(default_factory=list, description="Articles ou tomes listés")
    target_item_present: bool | None = Field(default=None, description="Présence du produit visé")
    estimated_price_per_item_cents: int | None = Field(
        default=None, description="Prix unitaire moyen"
    )
    summary: str | None = Field(default=None, description="Synthèse courte")


class ProfitScoringResult(BaseModel):
    """Scoring de rentabilité et indice pépite."""

    purchase_cost_cents: int | None = Field(
        default=None, description="Prix d'achat total estimé en centimes"
    )
    estimated_resale_cents: int | None = Field(default=None, description="Revente estimée")
    net_profit_cents: int | None = Field(default=None, description="Bénéfice net estimé")
    pepite_score: float | None = Field(default=None, description="Indice pépite de 1.0 à 10.0")
    verdict: str | None = Field(default=None, description="Conseil d'achat rapide")


class VisionPhotoInspection(BaseModel):
    """Analyse visuelle d'une photo individuelle."""

    photo_index: int = Field(default=1, description="Index de la photo (1-based)")
    photo_url: str = Field(default="", description="URL de la photo")
    defects: list[str] = Field(
        default_factory=list, description="Défauts visibles (accrocs, pliures...)"
    )
    spines_detected: list[str] = Field(
        default_factory=list, description="Titres/tomes sur la tranche"
    )
    is_suspicious_or_empty: bool = Field(default=False, description="Boîte vide ou non conforme")
    confidence: float = Field(default=0.0, description="Niveau de confiance de 0 à 1")


class VisionGuardAnalysisResult(BaseModel):
    """Résultat complet de l'inspection multi-photos VisionGuard."""

    has_visual_defects: bool = Field(default=False, description="True si au moins 1 défaut visuel")
    is_suspicious: bool = Field(default=False, description="True si arnaque/boîte vide suspectée")
    summary: str = Field(default="", description="Résumé synthétique pour Telegram")
    is_lot: bool = Field(
        default=False, description="True si plusieurs tomes/articles sont visibles ensemble (lot)"
    )
    identified_tome: int | None = Field(
        default=None, description="Numéro de tome identifié sur couverture/tranche"
    )
    identified_tomes: list[int] = Field(
        default_factory=list,
        description="Numéros de tomes identifiés si lot ou plusieurs tomes visibles",
    )
    identified_subtitle: str | None = Field(
        default=None, description="Sous-titre ou titre du tome identifié"
    )
    inspections: list[VisionPhotoInspection] = Field(default_factory=list)


class NegotiationResult(BaseModel):
    """Message d'offre et proposition de prix pour la négociation."""

    suggested_offer_cents: int = Field(description="Montant de l'offre en centimes")
    suggested_offer_euros: float = Field(description="Montant en euros")
    message: str = Field(description="Message prêt à être copié-collé sur Vinted")


class EnrichedPostAlertResult(BaseModel):
    """Résultat consolidé des enrichissements post-alerte."""

    price_glitch: PriceGlitchResult | None = None
    hidden_condition: HiddenConditionResult | None = None
    bundle_decomposition: BundleDecompositionResult | None = None
    profit_scoring: ProfitScoringResult | None = None
    vision_guard: VisionGuardAnalysisResult | None = None
