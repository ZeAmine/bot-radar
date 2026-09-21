"""Tests unitaires pour BundleDetector et le matching de lots."""

import uuid

from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.services.bundle_detector import BundleDetector
from app.services.matcher_service import evaluate_listing_against_alert


def test_bundle_detector_various_titles():
    # Détection par mot-clé explicite
    res1 = BundleDetector.detect("Lot de 3 jeux Switch Mario Zelda Pokémon")
    assert res1.is_bundle is True
    assert res1.estimated_item_count == 3

    res2 = BundleDetector.detect("Pack Nintendo Switch avec 5 jeux")
    assert res2.is_bundle is True
    assert res2.estimated_item_count == 5

    res3 = BundleDetector.detect("Collection intégrale 12 tomes One Piece")
    assert res3.is_bundle is True
    assert res3.estimated_item_count == 12

    # Article unitaire normal (pas un lot)
    res_single = BundleDetector.detect("Jeu Switch Mario Kart 8 Deluxe en boîte")
    assert res_single.is_bundle is False
    assert res_single.estimated_item_count is None


def test_bundle_matching_price_strict_threshold():
    """Vérifie que le seuil de prix fixé par l'utilisateur est respecté strictement (pas de multiplicateur automatique)."""
    cat = Category(id=uuid.uuid4(), slug="tech", label="Électronique", emoji="💻")
    alert = Alert(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Mario Kart 8 Deluxe",
        status=AlertStatus.ACTIVE,
        include_keywords=["mario kart"],
        exclude_keywords=[],
        variants=[],
        max_item_price_cents=3000,  # 30 € max fixé par l'utilisateur
        category=cat,
    )

    # Cas 1 : Lot à 50 € dépassant le seuil max fixé par l'utilisateur (doit être rejeté)
    match_lot_expensive = evaluate_listing_against_alert(
        alert=alert,
        listing_normalized_title="lot 3 jeux switch mario kart pokemon smash",
        listing_normalized_description="tres bon etat",
        listing_category_slug="tech",
        listing_condition="tres bon etat",
        listing_item_price_cents=5000,  # 50 €
    )
    assert match_lot_expensive.is_match is False
    assert match_lot_expensive.price_matches is False

    # Cas 2 : Lot respectant le prix max fixé par l'utilisateur (25 € <= 30 €)
    match_lot_ok = evaluate_listing_against_alert(
        alert=alert,
        listing_normalized_title="lot 3 jeux switch mario kart pokemon smash",
        listing_normalized_description="tres bon etat",
        listing_category_slug="tech",
        listing_condition="tres bon etat",
        listing_item_price_cents=2500,  # 25 €
    )
    assert match_lot_ok.is_match is True


def test_bundle_matching_bd_comics_strict():
    """Vérifie le respect strict du prix max pour les BDs."""
    cat = Category(id=uuid.uuid4(), slug="bd_comics", label="Livres", emoji="📚")
    alert = Alert(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Astérix - Tome 40",
        status=AlertStatus.ACTIVE,
        include_keywords=["astérix"],
        exclude_keywords=[],
        variants=[],
        isbn="9782014001150",
        max_item_price_cents=800,  # 8 € max
        category=cat,
    )

    # Lot de BDs Astérix à 25 € > 8 € (rejeté strictement)
    match_lot = evaluate_listing_against_alert(
        alert=alert,
        listing_normalized_title="lot 5 bd asterix en tres bon etat",
        listing_normalized_description="",
        listing_category_slug="bd_comics",
        listing_condition="tres bon etat",
        listing_item_price_cents=2500,  # 25 €
        listing_isbn13=None,
    )
    assert match_lot.is_match is False
    assert match_lot.price_matches is False

