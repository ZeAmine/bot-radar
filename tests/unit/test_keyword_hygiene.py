"""Le nettoyeur de mots-clés, testé sur les cas qui l'ont fait écrire.

Deux désastres réels servent de cas de référence : l'énumération du numéro d'un titre
(« … 2a » … « … 2z ») qui élargissait le matching à l'infini, et l'exclusion « 1 » nue
qui rejetait toute annonce chiffrée. Le module est pur : aucun mock n'est nécessaire.
"""

from __future__ import annotations

from app.domain.rules.keyword_hygiene import (
    MAX_EXCLUSIONS,
    MAX_VARIANTS,
    is_degenerate_exclusion,
    is_degenerate_variant,
    sanitize_exclusions,
    sanitize_variants,
)

TITRE_NUMEROTE = "Absolute Martian Manhunter Vol. 2"


def test_variants_number_enumeration_is_dropped():
    """Le cas qui a produit « … 2a » … « … 2z » : la famille entière disparaît."""
    family = [f"absolute martian manhunter 2{letter}" for letter in "abcdefghijklmnopqrstuvwxyz"]
    variants = sanitize_variants(
        [
            "absolute martian manhunter 2",
            "absolute martian manhunter tome 2",
            "absolute martian manhunter t2",
            *family,
        ],
        TITRE_NUMEROTE,
    )

    assert "absolute martian manhunter 2" in variants
    assert "absolute martian manhunter tome 2" in variants
    assert all(not is_degenerate_variant(v) for v in variants)
    assert len(variants) <= MAX_VARIANTS


def test_variants_with_full_title_and_letter_suffix_are_dropped():
    """« … vol 2z » répète le titre *et* énumère son numéro : les deux règles le coupent."""
    variants = sanitize_variants(
        ["absolute martian manhunter vol 2z", "absolute martian manhunter volume 2"],
        TITRE_NUMEROTE,
    )

    assert variants == ["absolute martian manhunter volume 2"]


def test_variant_extending_the_title_is_dropped():
    """« blasts » n'est pas une énumération, mais il répète « Blast » : il est écarté."""
    assert is_degenerate_variant("blasts") is False
    assert sanitize_variants(["blasts"], "Blast") == []


def test_variants_cap_and_normalised_dedup():
    """Au-delà du plafond, le moteur énumère ; deux écritures identiques n'en font qu'une."""
    many = [f"black sad variante {index}" for index in range(12)]
    assert len(sanitize_variants(many, "Blacksad")) == MAX_VARIANTS

    # « black sad » et « black  sad » se normalisent à l'identique : une seule survit.
    assert sanitize_variants(["black sad", "black  sad"], "Blacksad") == ["black sad"]


def test_variants_still_drop_isbn_and_repeated_title():
    """Les règles historiques tiennent : un code d'ouvrage ou le titre lui-même est rejeté."""
    variants = sanitize_variants(
        [
            "les enfants perdus 9782214080627",
            "978-2-214-08062-7",
            "les enfants perdus 1989",
            "les enfants perdus la pensée universelle",
            "enfants perdus",
            "les enfnats perdus",
        ],
        "Les Enfants perdus",
    )

    assert variants == ["les enfnats perdus"]


def test_exclusions_numeric_and_single_char_are_dropped():
    """Une exclusion est un rejet : « 1 » rejetterait toute annonce chiffrée."""
    exclusions = sanitize_exclusions(["1", "r", "tome 1", "hs", "coffret vide"], "Blacksad")

    assert exclusions == ["tome 1", "hs", "coffret vide"]
    assert is_degenerate_exclusion("1") is True
    assert is_degenerate_exclusion("hs") is False


def test_exclusions_cap_is_preserved():
    """Le plafond d'exclusions ne bouge pas."""
    many = [f"exclusion {index}" for index in range(10)]
    assert len(sanitize_exclusions(many, "Blacksad")) == MAX_EXCLUSIONS


def test_degenerate_variant_does_not_flag_a_model_suffix():
    """« 13mini » n'est pas une énumération : la lettre n'est pas seule après le chiffre."""
    assert is_degenerate_variant("iphone 13mini") is False
    assert is_degenerate_variant("one piece 2a") is True
