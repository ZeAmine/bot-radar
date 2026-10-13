"""Tests unitaires pour la normalisation de texte et titres."""

from app.services.matcher_service import normalize_text
from app.utils.text import compact_text, similarity


def test_normalization_accents():
    raw = "Éléphant à Noël, prêt pour l'été !"
    expected = "elephant a noel pret pour l ete"
    assert normalize_text(raw) == expected


def test_normalization_apostrophes():
    # Différents types d'apostrophes (typographique, standard, accent grave, etc.)
    raw = "l'arbre d’or l‘eau d`argent"
    expected = "l arbre d or l eau d argent"
    assert normalize_text(raw) == expected


def test_normalization_dashes():
    # Tiret classique, demi-cadratin, cadratin, tiret bas
    raw = "Blacksad - Intégrale – Tome 1 — Édition_Spéciale"
    expected = "blacksad integrale tome 1 edition speciale"
    assert normalize_text(raw) == expected


def test_normalization_spaces():
    raw = "   blacksad    intégrale \t\n  tome   1   "
    expected = "blacksad integrale tome 1"
    assert normalize_text(raw) == expected


def test_normalization_unicode():
    # Unicode avec caractères combinés (NFC vs NFD)
    raw_nfc = "\u00e9\u00e8\u00ea"  # é, è, ê précomposés sans espace
    raw_nfd = "e\u0301e\u0300e\u0302"  # e + accents combinés
    assert normalize_text(raw_nfc) == "eee"
    assert normalize_text(raw_nfd) == "eee"


def test_normalization_punctuation():
    raw = "Nintendo Switch (OLED) : Blanche ! Neuf ?"
    expected = "nintendo switch oled blanche neuf"
    assert normalize_text(raw) == expected


def test_compact_text_joins_the_writings_of_the_same_title():
    """Le tiret et l'espace se collent : trois graphies deviennent une seule forme.

    C'est ce que la normalisation seule ne fait pas — « one-piece » devient « one piece »
    mais « onepiece » reste un token distinct — et donc ce qui manque au matching par
    mots entiers pour retrouver un vendeur qui écrit le titre collé.
    """
    assert compact_text("One Piece") == "onepiece"
    assert compact_text("one-piece") == "onepiece"
    assert compact_text("onepiece") == "onepiece"
    assert compact_text("  Édition  Spéciale  ") == "editionspeciale"


def test_similarity_of_the_same_title_written_differently_is_one():
    assert similarity("One Piece", "onepiece") == 1.0
    assert similarity("Blade Runner 2049", "bladerunner 2049") == 1.0


def test_similarity_separates_a_typo_from_another_product():
    """« blacksat » est une faute de frappe ; « op » est un autre mot, pas une écriture."""
    assert similarity("blacksat", "Blacksad") >= 0.72
    assert similarity("one peice", "One Piece") >= 0.72
    assert similarity("op", "One Piece") < 0.72
    assert similarity("amm", "Absolute Martian Manhunter") < 0.72


def test_similarity_without_a_usable_form_is_zero():
    assert similarity("", "One Piece") == 0.0
    assert similarity(None, None) == 0.0
    assert similarity("   ", "One Piece") == 0.0
