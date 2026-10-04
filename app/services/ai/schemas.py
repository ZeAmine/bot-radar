"""Schémas Pydantic du sous-système IA, indépendants du moteur."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.services.book_metadata_service import EditionAlternative, WorkAlternative


class _LoosePayload(BaseModel):
    """JSON LLM : champs absents prennent le défaut, le surplus est ignoré."""

    model_config = ConfigDict(extra="ignore")


class BookContext(BaseModel):
    """Fiche réelle d'un ouvrage, source de vérité pour les mots-clés d'une alerte.

    Renseignée depuis la notice BNF : elle permet de raisonner sur l'ouvrage réel
    (auteur, édition, format) au lieu de recopier des listes de faux positifs types.
    """

    title: str = Field(description="Titre canonique de l'ouvrage")
    author: str | None = Field(default=None, description="Auteur principal")
    publisher: str | None = Field(default=None, description="Éditeur / collection")
    publication_year: int | None = Field(default=None, description="Année de la référence")
    format_detail: str | None = Field(default=None, description="Format éditorial (pages, cm)")
    isbn13: str | None = Field(default=None, description="ISBN de l'édition de référence")
    is_series: bool = Field(default=False, description="True pour une série multi-tomes")

    def as_facts(self) -> list[str]:
        """Énumère les faits bibliographiques exploitables par le modèle, en français."""
        facts = [f"titre officiel : {self.title}"]
        if self.author:
            facts.append(f"auteur : {self.author}")
        if self.publisher:
            facts.append(f"éditeur : {self.publisher}")
        if self.publication_year:
            facts.append(f"année de l'édition : {self.publication_year}")
        if self.format_detail:
            facts.append(f"format : {self.format_detail}")
        if self.isbn13:
            facts.append(f"ISBN de référence : {self.isbn13}")
        facts.append("nature : série multi-tomes" if self.is_series else "ouvrage unique")
        return facts


class NaturalAddResult(BaseModel):
    """Résultat du parsing en langage naturel d'une commande /add."""

    name: str = Field(description="Nom ou titre principal de l'alerte")
    category_slug: str = Field(
        default="bd_comics",
        description="Slug de catégorie: bd_comics, tech, gaming, clothes",
    )
    isbn13: str | None = Field(
        default=None, description="ISBN-13 confirmé par la BNF pour l'ouvrage demandé"
    )
    is_series: bool = Field(default=False, description="True s'il s'agit d'une série multi-tomes")
    author: str | None = Field(default=None, description="Auteur identifié")
    publisher: str | None = Field(default=None, description="Éditeur ou collection identifié")
    include_keywords: list[str] = Field(default_factory=list, description="Mots-clés obligatoires")
    exclude_keywords: list[str] = Field(default_factory=list, description="Mots-clés à exclure")
    max_price_cents: int | None = Field(default=None, description="Prix maximum en centimes")
    conditions: list[str] | None = Field(default=None, description="États acceptés")
    explanation: str | None = Field(default=None, description="Explication courte du découpage")
    isbn_note: str | None = Field(
        default=None,
        description="Information d'édition à afficher (autre ISBN connu / ISBN non confirmé)",
    )
    book_context: BookContext | None = Field(
        default=None,
        description="Fiche BNF de l'ouvrage, à injecter dans la génération de mots-clés",
    )
    edition_alternatives: list[EditionAlternative] = Field(
        default_factory=list,
        description="Éditions concurrentes de la même œuvre, sélectionnables dans le récap",
    )
    work_choices: list[WorkAlternative] = Field(
        default_factory=list,
        description=(
            "Autres ouvrages portant le même titre, sélectionnables dans le récap "
            "quand le titre seul ne désigne pas une œuvre unique"
        ),
    )


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
    rationale: str | None = None


class AutoTuningPayload(_LoosePayload):
    """Exclusions suggérées après des faux positifs."""

    suggested_exclusions: list[Any] | None = None
    explanation: str | None = None


class NegotiationResult(BaseModel):
    """Message d'offre et proposition de prix pour la négociation."""

    suggested_offer_cents: int = Field(description="Montant de l'offre en centimes")
    suggested_offer_euros: float = Field(description="Montant en euros")
    message: str = Field(description="Message prêt à être copié-collé sur Vinted")
