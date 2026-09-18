"""Tests unitaires pour la normalisation de texte et titres."""

from app.services.matcher_service import normalize_text


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
