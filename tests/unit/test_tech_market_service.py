"""Tests unitaires pour le service d'argus tech et estimation de marge nette."""

import uuid

from app.db.models.alert import Alert
from app.db.models.listing import Listing
from app.services.tech_market_service import TECH_BENCHMARKS, TechMarketService


def test_find_benchmark_exact():
    bench = TechMarketService.find_benchmark("Jeu Nintendo Switch Mario Kart 8 Deluxe comme neuf")
    assert bench is not None
    assert bench.product_name == "Mario Kart 8 Deluxe"
    assert bench.resale_target_cents == 3800


def test_find_benchmark_calculator():
    bench = TechMarketService.find_benchmark("Calculatrice TI 83 Premium CE Edition Python")
    assert bench is not None
    assert bench.product_name == "TI-83 Premium CE"
    assert bench.resale_target_cents == 5500


def test_get_comparison_summary_profitable():
    listing = Listing(
        id=uuid.uuid4(),
        category_slug="tech",
        title="Mario Kart 8 Deluxe Switch",
        item_price_cents=1800,  # 18.00 €
    )
    summary = TechMarketService.get_comparison_summary(listing)
    assert summary is not None
    assert "Cote revente :" in summary
    assert "38,00 €" in summary
    assert "Bénéfice estimé :" in summary
    # Resale = 38€, fees = 5% (1.90€), net profit = 38 - 18 - 1.90 = 18.10€
    assert "18,10 €" in summary


def test_get_comparison_summary_unprofitable():
    listing = Listing(
        id=uuid.uuid4(),
        category_slug="tech",
        title="Mario Kart 8 Deluxe Switch",
        item_price_cents=4000,  # Plus cher que la cote (38.00 €)
    )
    summary = TechMarketService.get_comparison_summary(listing)
    assert summary is None


def test_get_comparison_summary_with_alert_fallback():
    alert = Alert(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Accessoire Tech Générique",
        max_item_price_cents=2000,
    )
    listing = Listing(
        id=uuid.uuid4(),
        category_slug="tech",
        title="Accessoire Inconnu",
        item_price_cents=1200,
    )
    summary = TechMarketService.get_comparison_summary(listing, alert=alert)
    assert summary is not None
    assert "Cote revente :" in summary
    # 20€ * 1.35 = 27€ resale
    assert "27,00 €" in summary
