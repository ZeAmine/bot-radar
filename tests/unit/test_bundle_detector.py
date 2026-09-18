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


def test_bundle_matching_price_tolerance():
    cat = Category(id=uuid.uuid4(), slug="tech", label="Électronique", emoji="💻")
    alert = Alert(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Mario Kart 8 Deluxe",
        status=AlertStatus.ACTIVE,
        include_keywords=["mario kart"],
        exclude_keywords=[],
        variants=[],
        max_item_price_cents=3000,  # 30 € max pour 1 jeu
        category=cat,
    )

    # Cas 1 : Lot de 3 jeux à 50 € contenant Mario Kart
    # (Sans tolérance de lot, 50€ > 30€ serait rejeté)
    match_lot = evaluate_listing_against_alert(
        alert=alert,
        listing_normalized_title="lot 3 jeux switch mario kart pokemon smash",
        listing_normalized_description="tres bon etat",
        listing_category_slug="tech",
        listing_condition="tres bon etat",
        listing_item_price_cents=5000,  # 50 €
    )
    assert match_lot.is_match is True
    assert match_lot.is_bundle is True

    # Cas 2 : Article unitaire seul à 35 € (dépasse les 30 €, rejeté)
    match_single_too_expensive = evaluate_listing_against_alert(
        alert=alert,
        listing_normalized_title="mario kart 8 deluxe switch",
        listing_normalized_description="",
        listing_category_slug="tech",
        listing_condition="tres bon etat",
        listing_item_price_cents=3500,  # 35 €
    )
    assert match_single_too_expensive.is_match is False
    assert match_single_too_expensive.is_bundle is False


def test_bundle_matching_bd_comics():
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
        max_item_price_cents=800,  # 8 € max par album
        category=cat,
    )

    # Lot de 5 BDs Astérix à 25 € (prix unitaire 5 € < 8 €)
    match_lot = evaluate_listing_against_alert(
        alert=alert,
        listing_normalized_title="lot 5 bd asterix en tres bon etat",
        listing_normalized_description="",
        listing_category_slug="bd_comics",
        listing_condition="tres bon etat",
        listing_item_price_cents=2500,  # 25 €
        listing_isbn13=None,  # Pas d'ISBN dans l'annonce du lot
    )
    assert match_lot.is_match is True
    assert match_lot.is_bundle is True
