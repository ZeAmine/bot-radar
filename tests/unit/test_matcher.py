"""Tests unitaires pour le moteur de matching."""

import uuid

from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.services.matcher_service import evaluate_listing_against_alert


def create_test_alert(
    *,
    category_slug: str = "bd_comics",
    name: str = "Test Alert",
    include_keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    variants: list[str] | None = None,
    max_price_cents: int | None = 1500,
    accepted_conditions: list[str] | None = None,
    status: AlertStatus = AlertStatus.ACTIVE,
) -> Alert:
    category = Category(
        id=uuid.uuid4(),
        slug=category_slug,
        label="Test Cat",
        emoji="📚",
        is_active=True,
    )
    return Alert(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        category_id=category.id,
        name=name,
        include_keywords=include_keywords or ["blacksad"],
        exclude_keywords=exclude_keywords or [],
        variants=variants or [],
        max_item_price_cents=max_price_cents,
        accepted_conditions=accepted_conditions,
        status=status,
        category=category,
    )


def test_matcher_word_included():
    alert = create_test_alert(include_keywords=["blacksad", "intégrale"])
    res = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="blacksad integrale tome 1",
        listing_normalized_description="tres bon etat",
        listing_condition="Très bon état",
        listing_item_price_cents=1200,
    )
    assert res.is_match is True
    assert res.keyword_match_score >= 1


def test_matcher_word_excluded():
    alert = create_test_alert(
        include_keywords=["blacksad"],
        exclude_keywords=["poche", "tome 1"],
    )
    # L'annonce contient un mot exclu dans la description
    res = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="blacksad integrale",
        listing_normalized_description="edition poche souple",
        listing_condition="Bon état",
        listing_item_price_cents=1000,
    )
    assert res.is_match is False
    assert "exclu" in res.reason.lower()


def test_matcher_variants():
    alert = create_test_alert(
        include_keywords=["blacksad intégrale"],
        variants=["blacksad integral"],
    )
    # L'annonce utilise la variante
    res = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="blacksad integral luxe",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=1400,
    )
    assert res.is_match is True


def test_matcher_condition_accepted_and_rejected():
    alert = create_test_alert(
        accepted_conditions=["Neuf avec étiquette", "Très bon état"],
    )
    # État accepté
    res_ok = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="blacksad integrale",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=1000,
    )
    assert res_ok.is_match is True

    # État rejeté
    res_rejected = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="blacksad integrale",
        listing_normalized_description=None,
        listing_condition="Satisfaisant",
        listing_item_price_cents=1000,
    )
    assert res_rejected.is_match is False
    assert res_rejected.condition_matches is False


def test_matcher_price_under_and_over_threshold():
    alert = create_test_alert(max_price_cents=1500)

    # Sous le seuil (12,00 €)
    res_under = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="blacksad",
        listing_normalized_description=None,
        listing_condition="Bon état",
        listing_item_price_cents=1200,
    )
    assert res_under.is_match is True
    assert res_under.price_matches is True

    # Égal au seuil (15,00 €)
    res_equal = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="blacksad",
        listing_normalized_description=None,
        listing_condition="Bon état",
        listing_item_price_cents=1500,
    )
    assert res_equal.is_match is True

    # Au-dessus du seuil (15,01 €)
    res_over = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="blacksad",
        listing_normalized_description=None,
        listing_condition="Bon état",
        listing_item_price_cents=1501,
    )
    assert res_over.is_match is False
    assert res_over.price_matches is False


def test_matcher_paused_alert():
    alert = create_test_alert(status=AlertStatus.PAUSED)
    res = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="blacksad",
        listing_normalized_description=None,
        listing_condition="Bon état",
        listing_item_price_cents=1000,
    )
    assert res.is_match is False
    assert "non active" in res.reason.lower()


def test_matcher_multi_keywords_must_all_match():
    alert = create_test_alert(
        include_keywords=["batman", "terre", "integrale"],
        name="Batman Terre Un",
    )

    # Cas 1 : Seulement "integrale" présent (faux positif Bec-en-fer) -> DOIT ÉCHOUER
    res1 = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="BD intégrale Bec-en-fer",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=590,
    )
    assert res1.is_match is False

    # Cas 2 : Seulement "batman" présent (faux positif 100 couvertures) -> DOIT ÉCHOUER
    res2 = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="Livre batman 100 couvertures emblématiques 1939-2017",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=800,
    )
    assert res2.is_match is False

    # Cas 3 : Les 3 mots-clés sont présents -> DOIT RÉUSSIR
    res3 = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="Batman : Terre Un — Intégrale tome 1",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=1500,
    )
    assert res3.is_match is True


def test_matcher_word_boundaries_no_substring_confusion():
    alert = create_test_alert(include_keywords=["rip"])

    # "strip" ne doit pas matcher "rip"
    res_strip = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="BD Strip Tease",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=500,
    )
    assert res_strip.is_match is False

    # "RIP" en mot entier doit matcher
    res_rip = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="BD RIP tome 1",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=1500,
    )
    assert res_rip.is_match is True


def test_matcher_phrase_exclusion_blocks_simpson():
    alert = create_test_alert(
        include_keywords=["la route"],
        exclude_keywords=["sur la route", "simpson"],
        name="La Route — Manu Larcenet",
    )
    res = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="Bande dessinée les Simpson 22 sur la route",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=400,
    )
    assert res.is_match is False


def test_matcher_isbn_hybrid_fallback():
    # Alerte créée avec ISBN et nom distinctif "Soli Deo Gloria"
    alert = create_test_alert(
        name="Soli Deo Gloria",
        include_keywords=["9791034768974"],
        max_price_cents=2000,
    )
    alert.isbn = "9791034768974"

    # 1. Annonce avec ISBN exact dans le titre ou métadonnée -> Match direct
    res_direct = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="bd soli deo gloria 9791034768974",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=1500,
    )
    assert res_direct.is_match is True

    # 2. Annonce d'un vendeur sans ISBN (titre seul) -> Match via Fallback hybride
    res_hybrid = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="bd_comics",
        listing_normalized_title="bd soli deo gloria parfait etat",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=1200,
    )
    assert res_hybrid.is_match is True

    # 3. Annonce hors catégorie BD -> Rejeté
    res_wrong_cat = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="vetements",
        listing_normalized_title="t shirt soli deo gloria",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=1000,
    )
    assert res_wrong_cat.is_match is False

    # 4. Titre court : « Intégrale » accentué compte comme contexte BD
    alert_rip = create_test_alert(
        name="RIP",
        include_keywords=["rip"],
        max_price_cents=2000,
    )
    alert_rip.isbn = "9782205078961"
    res_integrale = evaluate_listing_against_alert(
        alert=alert_rip,
        listing_category_slug="bd_comics",
        listing_normalized_title="rip",
        listing_normalized_description="Intégrale",
        listing_condition="Très bon état",
        listing_item_price_cents=1200,
    )
    assert res_integrale.is_match is True


def test_matcher_title_with_author_separator_matches_catalog_items():
    # 1. Alerte 1984 — Xavier Coste contre annonce "1984 en BD"
    alert_1984 = create_test_alert(
        name="1984 — Xavier Coste",
        include_keywords=["1984"],
        max_price_cents=5000,
        accepted_conditions=["Très bon état"],
    )
    alert_1984.isbn = "9782377316588"

    res_1984 = evaluate_listing_against_alert(
        alert=alert_1984,
        listing_category_slug="bd_comics",
        listing_normalized_title="1984 en bd",
        listing_normalized_description="Exemplaire 1984 en BD en état Très bon état.",
        listing_condition="Très bon état",
        listing_item_price_cents=800,
    )
    assert res_1984.is_match is True

    # 2. Alerte Tout seul — Chabouté contre annonce "Tout seul"
    alert_seul = create_test_alert(
        name="Tout seul — Chabouté",
        include_keywords=["tout seul"],
        max_price_cents=5000,
        accepted_conditions=["Très bon état"],
    )
    alert_seul.isbn = "9782749304298"

    res_seul = evaluate_listing_against_alert(
        alert=alert_seul,
        listing_category_slug="bd_comics",
        listing_normalized_title="tout seul",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=1200,
    )
    assert res_seul.is_match is True

    # 3. Alerte La Route — Manu Larcenet contre annonce "BD La route" (15 € <= 18 €)
    alert_route = create_test_alert(
        name="La Route — Manu Larcenet",
        include_keywords=["la route"],
        max_price_cents=1800,
        accepted_conditions=["Très bon état"],
    )
    alert_route.isbn = "9782205208153"

    res_route_ok = evaluate_listing_against_alert(
        alert=alert_route,
        listing_category_slug="bd_comics",
        listing_normalized_title="bd la route",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=1500,
    )
    assert res_route_ok.is_match is True

    # 4. Alerte La Route — Manu Larcenet contre annonce à 20 € (> 18 € max) -> Rejetée par le prix
    res_route_overprice = evaluate_listing_against_alert(
        alert=alert_route,
        listing_category_slug="bd_comics",
        listing_normalized_title="la route de manu larcenet",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=2000,
    )
    assert res_route_overprice.is_match is False
    assert res_route_overprice.price_matches is False
    assert "Prix supérieur au seuil maximum" in res_route_overprice.reason


def test_matcher_clean_title_with_author_keywords():
    """Vérifie qu'une alerte avec un titre court épuré ('1984') matche
    grâce aux mots-clés de l'auteur dans include_keywords."""
    alert_1984 = create_test_alert(
        name="1984",
        include_keywords=["1984", "coste"],
        max_price_cents=5000,
        accepted_conditions=["Très bon état"],
    )
    alert_1984.isbn = "9782377316588"

    # Match grâce à la présence du mot-clé auteur 'coste'
    res_author_match = evaluate_listing_against_alert(
        alert=alert_1984,
        listing_category_slug="bd_comics",
        listing_normalized_title="1984 de xavier coste",
        listing_normalized_description="Édition Sarbacane en parfait état",
        listing_condition="Très bon état",
        listing_item_price_cents=1200,
    )
    assert res_author_match.is_match is True

    # Rejet si roman George Orwell sans contexte BD ni mot-clé auteur
    res_unrelated_novel = evaluate_listing_against_alert(
        alert=alert_1984,
        listing_category_slug="bd_comics",
        listing_normalized_title="1984 de george orwell poche",
        listing_normalized_description="Livre classique folio",
        listing_condition="Très bon état",
        listing_item_price_cents=500,
    )
    assert res_unrelated_novel.is_match is False


def test_matcher_comic_franchise_prefix_not_false_positive():
    """Vérifie que Spider-Man - Spider-Man n'est plus ! ne déclenche PAS l'alerte Kraven."""
    alert_kraven = create_test_alert(
        name="Spider-Man — La dernière chasse de Kraven",
        include_keywords=["kraven"],
        max_price_cents=1500,
        accepted_conditions=["Très bon état"],
    )
    alert_kraven.isbn = "9791039147125"

    # Annonce d'une autre BD Spider-Man sans mot-clé Kraven -> REJET
    res_other = evaluate_listing_against_alert(
        alert=alert_kraven,
        listing_category_slug="bd_comics",
        listing_normalized_title="spider man spider man n est plus",
        listing_normalized_description=(
            "Exemplaire Spider-Man - Spider-Man n'est plus ! "
            "en état Très bon état."
        ),
        listing_condition="Très bon état",
        listing_item_price_cents=800,
    )
    assert res_other.is_match is False
    assert (
        "mots-clés requis absents" in res_other.reason.lower()
        or "non satisfait" in res_other.reason.lower()
    )

    # Annonce de la bonne BD Kraven -> MATCH
    res_kraven = evaluate_listing_against_alert(
        alert=alert_kraven,
        listing_category_slug="bd_comics",
        listing_normalized_title="spider man la derniere chasse de kraven",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=1000,
    )
    assert res_kraven.is_match is True


def test_matcher_multilingual_exclusions_spanish_empty_box():
    """Vérifie qu'une annonce espagnole avec 'caja vacia'
    est rejetée par l'exclusion 'boite vide'."""
    alert = create_test_alert(
        category_slug="gaming",
        include_keywords=["mario kart 8 deluxe"],
        exclude_keywords=["boite vide"],
    )
    res = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="gaming",
        listing_normalized_title="caja vacia mario kart 8 deluxe switch",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=800,
    )
    assert res.is_match is False
    assert "exclu" in res.reason.lower()


def test_matcher_multilingual_exclusions_german_power_supply():
    """Vérifie qu'un chargeur allemand 'netzteil' est rejeté pour une alerte Raspberry Pi."""
    alert = create_test_alert(
        category_slug="tech",
        include_keywords=["raspberry", "pi 5"],
        exclude_keywords=["chargeur"],
    )
    res = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="tech",
        listing_normalized_title="raspberry pi 5 netzteil 27w",
        listing_normalized_description=None,
        listing_condition="Très bon état",
        listing_item_price_cents=900,
    )
    assert res.is_match is False
    assert "exclu" in res.reason.lower()


def test_matcher_gaming_rejection_foreign_spanish_game():
    """Vérifie qu'un jeu vidéo en espagnol (non-PAL-FR) est rejeté dans la catégorie gaming."""
    alert = create_test_alert(
        category_slug="gaming",
        include_keywords=["mario kart 8 deluxe"],
        exclude_keywords=[],
    )
    res = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="gaming",
        listing_normalized_title="mario kart 8 deluxe switch juego",
        listing_normalized_description="Juego en versión española como nuevo hago envios.",
        listing_condition="Très bon état",
        listing_item_price_cents=1500,
    )
    assert res.is_match is False
    assert "pal-fr" in res.reason.lower() or "étrangère" in res.reason.lower()


def test_matcher_gaming_acceptance_pal_fr():
    """Vérifie qu'un jeu vidéo français / PAL-FR est accepté dans la catégorie gaming."""
    alert = create_test_alert(
        category_slug="gaming",
        max_price_cents=2500,
        include_keywords=["mario kart 8 deluxe"],
        exclude_keywords=["boite vide"],
    )
    res = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="gaming",
        listing_normalized_title="jeu nintendo switch mario kart 8 deluxe complet",
        listing_normalized_description="Version française en parfait état, fonctionne très bien.",
        listing_condition="Très bon état",
        listing_item_price_cents=1800,
    )
    assert res.is_match is True


def test_matcher_tech_hardware_allows_spanish_listing():
    """Vérifie que le matériel tech (ex: Raspberry Pi) reste accepté même venant d'Espagne."""
    alert = create_test_alert(
        category_slug="tech",
        max_price_cents=5000,
        include_keywords=["raspberry", "pi 4"],
        exclude_keywords=["boitier", "coque"],
    )
    res = evaluate_listing_against_alert(
        alert=alert,
        listing_category_slug="tech",
        listing_normalized_title="raspberry pi 4 4gb",
        listing_normalized_description=(
            "Placa raspberry pi 4 4gb ram como nuevo, "
            "funcionando perfectamente."
        ),
        listing_condition="Très bon état",
        listing_item_price_cents=3500,
    )
    assert res.is_match is True


def test_pure_predicates_price_condition_exclusion_isbn():
    """Les prédicats extraits reproduisent les seuils utilisés par le matcher."""
    from app.services.matcher_service import (
        condition_is_accepted,
        default_exclusion_hit,
        first_matching_exclusion,
        isbn_digits,
        price_is_above_max,
    )

    assert price_is_above_max(1500, 1501) is True
    assert price_is_above_max(1500, 1500) is False
    assert price_is_above_max(None, 99999) is False

    assert condition_is_accepted(None, "Bon état") is True
    assert condition_is_accepted(["Très bon état"], None) is False
    assert condition_is_accepted(["Très bon état"], "very_good") is True
    assert condition_is_accepted(["Très bon état"], "Bon état") is False

    assert first_matching_exclusion("poster original tintin", ["poster", "figurine"]) == "poster"
    assert first_matching_exclusion("tintin au tibet", ["poster"]) is None

    assert (
        default_exclusion_hit("ex-libris tintin", ["ex-libris"], include_keywords=["tintin"])
        == "ex-libris"
    )
    assert (
        default_exclusion_hit("ex-libris tintin", ["ex-libris"], include_keywords=["ex-libris"])
        is None
    )
    assert (
        default_exclusion_hit("rip tome 1 strip tease", ["rip"], include_keywords=["strip tease"])
        == "rip"
    )
    assert (
        default_exclusion_hit("titre tintin boite vide", ["boite vide"], include_keywords=None)
        == "boite vide"
    )

    assert isbn_digits("978-2-205-07805-3") == "9782205078053"
    assert isbn_digits(None) == ""


