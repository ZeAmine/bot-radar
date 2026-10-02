"""Règles pures de classification des offres de marché d'occasion.

Source unique de vérité partagée par :

- `app.providers.registry` (sélection de la meilleure offre au refresh),
- `app.providers.chasse_aux_livres` (filtrage des offres agrégées),
- `app.services.market_price_service` (affichage Telegram de la cote).

Une cote n'a de valeur que si l'exemplaire est de l'occasion en état correct :
le Neuf est exclu, et un état déclaré inférieur à « Très bon » est refusé. Un état
non renseigné (« Occasion », `None`) reste acceptable : on ne rejette jamais une
offre d'occasion simplement parce que le marchand ne détaille pas l'exemplaire.
"""

import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.providers.base import MarketOffer

# Palier d'état d'un exemplaire d'occasion, du plus favorable au moins bon.
ConditionTier = Literal["brand_new", "like_new", "very_good", "acceptable", "unknown"]

# Marchands non francophones ou non pertinents à exclure formellement du comparateur.
EXCLUDED_MERCHANTS: frozenset[str] = frozenset({
    "amazon (de)",
    "amazon_de",
})

# Un marchand francophone qui ne qualifie pas l'exemplaire : l'état reste inconnu.
_UNQUALIFIED_MARKERS = ("occasion", "used", "non specifie")

# États explicitement inférieurs à « Très bon » (Bon, Correct, Moyen, usagé...).
_INFERIOR_KEYWORDS = (
    "correct",
    "acceptable",
    "moyen",
    "passable",
    "usag",
    "defraichi",
)

_LIKE_NEW_PATTERN = re.compile(
    r"\b(comme\s*neuf|like\s*new|near\s*mint|parfait|neuf\s*sous\s*blister)\b"
)
_VERY_GOOD_PATTERN = re.compile(r"\b(tr[eè]s\s*bon|very\s*good|tb\+?)\b")


def is_merchant_excluded(merchant_name: str | None) -> bool:
    """Vérifie si un marchand doit être strictement exclu (ex: Amazon DE / Allemagne)."""
    if not merchant_name:
        return False
    name_clean = merchant_name.strip().lower()
    if name_clean in EXCLUDED_MERCHANTS:
        return True
    is_amazon_de = "(de)" in name_clean or "_de" in name_clean or "-de" in name_clean
    return "amazon" in name_clean and is_amazon_de


def is_brand_new_condition(condition_str: str | None) -> bool:
    """Détecte un exemplaire vendu neuf, à exclure de la cote d'occasion.

    « Comme neuf » et « Like new » ne sont pas du neuf : ce sont des exemplaires
    d'occasion revendus par un particulier ou un professionnel.
    """
    if not condition_str:
        return False
    cond = condition_str.strip().lower()
    if not cond or "comme" in cond or "like" in cond:
        return False
    return cond in ("neuf", "new") or cond.startswith("neuf ") or cond.endswith(" neuf")


def is_brand_new_partner(p_data: dict[str, object]) -> bool:
    """Détecte si une offre partenaire (payload d'un agrégateur) est du Neuf."""
    if not p_data.get("is_used", True):
        return True
    return is_brand_new_condition(str(p_data.get("condition") or ""))


def condition_tier(condition_str: str | None) -> ConditionTier:
    """Classe un libellé d'état d'occasion en palier comparable ou non.

    - `brand_new` : exemplaire neuf, jamais retenu comme cote d'occasion ;
    - `like_new` / `very_good` : seuls paliers retenus pour la cote d'un tome ;
    - `unknown` : le marchand ne qualifie pas l'exemplaire (« Occasion ») ;
    - `acceptable` : état déclaré et inférieur à « Très bon », refusé.
    """
    if not condition_str:
        return "unknown"
    cond = condition_str.strip().lower()
    if not cond:
        return "unknown"
    if is_brand_new_condition(cond):
        return "brand_new"
    if _LIKE_NEW_PATTERN.search(cond):
        return "like_new"
    if _VERY_GOOD_PATTERN.search(cond):
        return "very_good"
    if any(marker in cond for marker in _UNQUALIFIED_MARKERS):
        return "unknown"
    if any(keyword in cond for keyword in _INFERIOR_KEYWORDS):
        return "acceptable"
    return "acceptable"


def is_very_good_or_better(condition_str: str | None) -> bool:
    """Vérifie si l'état d'un exemplaire est au moins « Très bon ».

    Exclut strictement : Bon, Correct, Acceptable, Moyen, etc.
    """
    return condition_tier(condition_str) in ("like_new", "very_good")


def is_acceptable_secondhand_condition(condition_str: str | None) -> bool:
    """Vrai si l'état déclaré permet de fonder une cote d'occasion dessus.

    Regroupe « Très bon » / « Comme neuf » et les états non qualifiés : le
    marchand ne détaille pas l'exemplaire mais vend bien de l'occasion.
    """
    return condition_tier(condition_str) in ("like_new", "very_good", "unknown")


def normalize_secondhand_condition(cond: str | None) -> str:
    """Normalise un libellé d'état pour un affichage compact et homogène.

    Les libellés libres des marchands (« Très bon état », « TB ») sont ramenés à
    « Très bon » ou « Comme neuf » pour que le comparatif par boutique reste
    lisible. Un état non normalisable est renvoyé tel quel plutôt que déformé.
    """
    tier = condition_tier(cond)
    if tier == "like_new":
        return "Comme neuf"
    if tier == "very_good":
        return "Très bon"
    return (cond or "").strip() or "Occasion"


def is_comparable_secondhand_offer(offer: "MarketOffer") -> bool:
    """Filtre unique appliqué à toute offre de marché avant d'entrer dans une cote.

    Garantit qu'un prix catalogue en « Bon état » ou une annonce neuve ne peut
    jamais devenir le prix de référence affiché dans une alerte.
    """
    if not offer.is_comparable:
        return False
    if is_merchant_excluded(offer.provider_name):
        return False
    return is_acceptable_secondhand_condition(offer.item_condition)
