"""Tests des règles pures de classification des offres de marché d'occasion."""

from app.core.constants import MatchConfidence
from app.domain.rules.market_offer import (
    condition_tier,
    is_acceptable_secondhand_condition,
    is_brand_new_condition,
    is_brand_new_partner,
    is_comparable_secondhand_offer,
    is_merchant_excluded,
    is_very_good_or_better,
    normalize_secondhand_condition,
)
from app.providers.base import MarketOffer


def _offer(condition: str | None, delivered: int = 1000, **kwargs: object) -> MarketOffer:
    return MarketOffer(
        provider_name=str(kwargs.pop("provider_name", "momox")),
        item_price_cents=delivered,
        shipping_price_cents=0,
        delivered_price_cents=delivered,
        source_url="https://example.com/1",
        item_condition=condition,
        confidence=MatchConfidence.HIGH,
        **kwargs,  # type: ignore[arg-type]
    )


def test_condition_tier_classifies_known_labels():
    assert condition_tier("Neuf") == "brand_new"
    assert condition_tier("Neuf sous blister") == "brand_new"
    assert condition_tier("Comme neuf") == "like_new"
    assert condition_tier("Comme neuf sous blister") == "like_new"
    assert condition_tier("Très bon état") == "very_good"
    assert condition_tier("Tres bon") == "very_good"
    assert condition_tier("Bon état") == "acceptable"
    assert condition_tier("Correct") == "acceptable"
    assert condition_tier("État moyen") == "acceptable"
    assert condition_tier("Occasion") == "unknown"
    assert condition_tier(None) == "unknown"


def test_brand_new_never_counts_as_secondhand():
    assert is_brand_new_condition("Neuf") is True
    assert is_brand_new_condition("New") is True
    # « Comme neuf » reste de l'occasion
    assert is_brand_new_condition("Comme neuf") is False
    assert is_brand_new_condition("Like new") is False
    assert is_brand_new_condition("Très bon") is False
    assert is_brand_new_partner({"is_used": False, "condition": "Très bon"}) is True
    assert is_brand_new_partner({"is_used": True, "condition": "Neuf"}) is True
    assert is_brand_new_partner({"is_used": True, "condition": "Très bon"}) is False


def test_very_good_gate_rejects_inferior_states():
    assert is_very_good_or_better("Très bon") is True
    assert is_very_good_or_better("Comme neuf") is True
    assert is_very_good_or_better("Bon") is False
    assert is_very_good_or_better("Acceptable") is False
    assert is_very_good_or_better("usagé") is False
    assert is_very_good_or_better("Neuf") is False
    assert is_very_good_or_better(None) is False


def test_unknown_state_stays_acceptable_but_lower_states_do_not():
    # Un marchand qui ne qualifie pas l'exemplaire vend bien de l'occasion
    assert is_acceptable_secondhand_condition("Occasion") is True
    assert is_acceptable_secondhand_condition(None) is True
    assert is_acceptable_secondhand_condition("Bon état") is False
    assert is_acceptable_secondhand_condition("Neuf") is False


def test_normalize_condition_keeps_unusual_labels():
    assert normalize_secondhand_condition("Très bon état +") == "Très bon"
    assert normalize_secondhand_condition("Comme neuf sous blister") == "Comme neuf"
    # Un état inattendu reste inchangé, pas déformé en « Très bon »
    assert normalize_secondhand_condition("État Satisfaction") == "État Satisfaction"
    assert normalize_secondhand_condition(None) == "Occasion"


def test_merchant_exclusion_covers_amazon_de():
    assert is_merchant_excluded("Amazon (De)") is True
    assert is_merchant_excluded("AMAZON_DE") is True
    assert is_merchant_excluded("amazon-de") is True
    assert is_merchant_excluded("Amazon.fr") is False
    assert is_merchant_excluded("Rakuten") is False
    assert is_merchant_excluded(None) is False


def test_offer_filter_blocks_new_inferior_and_excluded_merchants():
    assert is_comparable_secondhand_offer(_offer("Très bon")) is True
    assert is_comparable_secondhand_offer(_offer("Occasion")) is True
    # Un prix catalogue en « Bon état » ne doit jamais devenir une cote
    assert is_comparable_secondhand_offer(_offer("Bon état")) is False
    assert is_comparable_secondhand_offer(_offer("Neuf")) is False
    assert is_comparable_secondhand_offer(_offer("Très bon", provider_name="Amazon_DE")) is False
    assert is_comparable_secondhand_offer(_offer("Très bon", is_comparable=False)) is False
