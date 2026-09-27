"""Réexport des règles de matching. L'implémentation vit dans le domaine."""

from app.domain.rules.listing_match import (
    CONDITION_ALIASES,
    DEFAULT_BD_GOODIES_EXCLUSIONS,
    FRANCHISE_PREFIXES,
    MatchResult,
    condition_is_accepted,
    default_exclusion_hit,
    evaluate_listing_against_alert,
    expand_exclusions_multilingual,
    first_matching_exclusion,
    isbn_digits,
    price_is_above_max,
)
from app.utils.text import normalize_text

__all__ = [
    "CONDITION_ALIASES",
    "DEFAULT_BD_GOODIES_EXCLUSIONS",
    "FRANCHISE_PREFIXES",
    "MatchResult",
    "condition_is_accepted",
    "default_exclusion_hit",
    "evaluate_listing_against_alert",
    "expand_exclusions_multilingual",
    "first_matching_exclusion",
    "isbn_digits",
    "normalize_text",
    "price_is_above_max",
]
