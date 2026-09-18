"""Tests unitaires pour le calcul du prix total estimé."""

from app.services.price_calculator import (
    calculate_estimated_total,
    format_price_summary,
)


def test_calculate_total_all_known():
    # 900 (article) + 70 (protection) + 399 (port) = 1369 centimes
    total = calculate_estimated_total(
        item_price_cents=900,
        estimated_protection_cents=70,
        estimated_shipping_cents=399,
    )
    assert total == 1369


def test_calculate_total_unknown_shipping_or_protection():
    # Frais de port inconnu -> Total null
    assert calculate_estimated_total(900, 70, None) is None

    # Protection inconnue -> Total null
    assert calculate_estimated_total(900, None, 399) is None

    # Tous deux inconnus -> Total null
    assert calculate_estimated_total(900, None, None) is None


def test_format_price_summary():
    # Total calculé
    summary_known = format_price_summary(item_price_cents=900, estimated_total_cents=1369)
    assert summary_known == "💵 9,00 € (Total : 13,69 €)"

    # Total inconnu / à confirmer
    summary_unknown = format_price_summary(item_price_cents=1500, estimated_total_cents=None)
    assert summary_unknown == "💵 15,00 € (Total : à confirmer)"
