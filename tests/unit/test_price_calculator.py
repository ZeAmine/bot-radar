"""Tests unitaires pour le calcul du prix total estimé (Prix TTC précis)."""

from app.services.price_calculator import (
    calculate_buyer_protection_cents,
    calculate_estimated_total,
    format_price_summary,
)


def test_calculate_buyer_protection():
    # 15,00 € : 0,70 € + 5% (0,75 €) = 1,45 € (145 centimes)
    assert calculate_buyer_protection_cents(1500) == 145
    # 10,00 € : 0,70 € + 5% (0,50 €) = 1,20 € (120 centimes)
    assert calculate_buyer_protection_cents(1000) == 120


def test_calculate_total_all_known():
    # 900 (article) + 70 (protection) + 399 (port) = 1369 centimes
    total = calculate_estimated_total(
        item_price_cents=900,
        estimated_protection_cents=70,
        estimated_shipping_cents=399,
    )
    assert total == 1369


def test_calculate_total_unknown_shipping_or_protection():
    # Sans fallback : total null
    assert calculate_estimated_total(900, 70, None) is None
    assert calculate_estimated_total(900, None, 399) is None
    assert calculate_estimated_total(900, None, None) is None

    # Avec fallback : barème standard Vinted
    # 1500 + 145 (protection) + 288 (port) = 1933 centimes (19,33 €)
    fallback_total = calculate_estimated_total(
        item_price_cents=1500,
        fallback_defaults=True,
    )
    assert fallback_total == 1933


def test_format_price_summary():
    # Format de charte : le prix d'abord, le total entre parenthèses, marqué « TTC ».
    # Total fourni explicitement : 9,00 € + frais = 13,69 € estimés
    summary_known = format_price_summary(item_price_cents=900, estimated_total_cents=1369)
    assert summary_known == "<b>Prix :</b> 9,00 € (13,69 € TTC)"

    # Total manquant : calcul automatique du total estimé (15,00 € -> 19,33 €)
    summary_auto_fallback = format_price_summary(item_price_cents=1500, estimated_total_cents=None)
    assert summary_auto_fallback == "<b>Prix :</b> 15,00 € (19,33 € TTC)"

    # Total manquant sans fallback : pas de total inventé
    summary_no_fallback = format_price_summary(
        item_price_cents=1500,
        estimated_total_cents=None,
        fallback_to_default_fees=False,
    )
    assert summary_no_fallback == "<b>Prix :</b> 15,00 €"
