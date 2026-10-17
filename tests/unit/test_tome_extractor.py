"""Tests unitaires pour le module d'extraction de tomes et de lots TomeExtractor."""

import pytest

from app.services.tome_extractor import TomeExtractor


@pytest.mark.parametrize(
    ("title", "series_key", "expected_tome"),
    [

        ("BD RIP Tome 2", "RIP", 2),
        ("RIP T3", "RIP", 3),
        ("RIP t.3", "RIP", 3),
        ("Blacksad vol. 4", "Blacksad", 4),
        ("Blast Volume 1", "Blast", 1),
        ("L'odyssée d'Hakim livre 3", "L'odyssée d'Hakim", 3),
        ("RIP #5", "RIP", 5),
        ("RIP # 5", "RIP", 5),

        ("RIP Tome IV", "RIP", 4),
        ("Blacksad Tome II", "Blacksad", 2),
        ("Blast Tome VI", "Blast", 6),
        ("Série Tome X", None, 10),

        ("RIP 2", "RIP", 2),
        ("Blacksad 3", "Blacksad", 3),
    ],
)
def test_tome_extractor_single_tome_matrix(
    title: str, series_key: str | None, expected_tome: int
) -> None:
    """Matrice générale pour l'extraction d'un tome unique (arabes, romains, suffixes)."""
    res = TomeExtractor.extract(title, series_key=series_key)
    assert res.is_lot is False
    assert res.tome_number == expected_tome


@pytest.mark.parametrize(
    ("title", "series_key", "expected_is_lot", "expected_lot_tomes", "expected_count"),
    [

        ("Le château des animaux,  1.2.3", "Le château des animaux", True, [1, 2, 3], 3),
        ("Série BD 1. 2. 3", None, True, [1, 2, 3], 3),
        ("Manga 1.2.3.4", None, True, [1, 2, 3, 4], 4),

        ("Le château des animaux 1, 2, 3", "Le château des animaux", True, [1, 2, 3], 3),
        ("Comics tomes 1, 2, 4", None, True, [1, 2, 4], 3),  # Lot non-consécutif

        ("Le château des animaux 1-2-3", "Le château des animaux", True, [1, 2, 3], 3),
        ("Tintin 4-5-6", None, True, [4, 5, 6], 3),

        ("Le château des animaux 1/2/3", "Le château des animaux", True, [1, 2, 3], 3),
        ("BD 1+2+3", None, True, [1, 2, 3], 3),
        ("Spirou 1 & 2", None, True, [1, 2], 2),
        ("Largo Winch 1 et 2", None, True, [1, 2], 2),
        ("Thorgal t1-2-3", None, True, [1, 2, 3], 3),

        ("Le château des animaux 1 à 3", "Le château des animaux", True, [1, 2, 3], 3),
        ("Le château des animaux 1-3", "Le château des animaux", True, [1, 2, 3], 3),
        ("Lot BD RIP tomes 1 à 4", "RIP", True, [1, 2, 3, 4], 4),
        ("RIP tomes 1-3", "RIP", True, [1, 2, 3], 3),
        ("BD 1 a 3", None, True, [1, 2, 3], 3),
        ("BD 1 au 3", None, True, [1, 2, 3], 3),
        ("Manga tomes 5 à 8", None, True, [5, 6, 7, 8], 4),
        # Énumération explicite
        ("BD RIP T1 T2 T3", "RIP", True, [1, 2, 3], 3),
        ("Le château des animaux tomes 1, 2 et 3", "Le château des animaux", True, [1, 2, 3], 3),
        ("Tome 1, tome 2", None, True, [1, 2], 2),
        # Lots généraux sans numéros précis
        ("Lot 3 BD RIP", "RIP", True, [], 3),
        ("Coffret RIP intégrale", "RIP", True, [], None),
        ("Slava (3 volumes) Dargaud", "Slava", True, [], 3),
    ],
)
def test_tome_extractor_lot_detection_matrix(
    title: str,
    series_key: str | None,
    expected_is_lot: bool,
    expected_lot_tomes: list[int],
    expected_count: int | None,
) -> None:
    """Matrice générale pour la détection de lots et bundles multi-tomes."""
    res = TomeExtractor.extract(title, series_key=series_key)
    assert res.is_lot == expected_is_lot
    if expected_lot_tomes:
        assert res.lot_tomes == expected_lot_tomes
    if expected_count is not None:
        assert res.lot_count == expected_count


@pytest.mark.parametrize(
    ("title", "series_key", "expected_tome", "expected_sub"),
    [
        ("BD RIP - Maurice", "RIP", 2, "maurice"),
        ("RIP Ahmed", "RIP", 3, "ahmed"),
        ("Blacksad Arctic-Nation", "Blacksad", 2, "arctic-nation"),
        ("Blast Grasse Carcasse", "Blast", 1, "grasse carcasse"),
        ("1629 Apothicaire du diable", "1629", 1, "apothicaire du diable"),
        (
            "Le combat ordinaire - Les quantités négligeables",
            "Le combat ordinaire",
            2,
            "quantites negligeables",
        ),
    ],
)
def test_tome_extractor_subtitles_matrix(
    title: str, series_key: str, expected_tome: int, expected_sub: str
) -> None:
    """Matrice générale pour la reconnaissance de tomes via sous-titres officiels."""
    res = TomeExtractor.extract(title, series_key=series_key)
    assert res.is_lot is False
    assert res.tome_number == expected_tome
    assert res.matched_subtitle == expected_sub


@pytest.mark.parametrize(
    "false_positive_title",
    [
        # Prix en euros
        "BD RIP 15€",
        "BD RIP 20 euros bon état",
        "Livre jeunesse 1.50 €",
        "Roman 9.99 EUR",
        # Formats et dimensions physiques
        "BD format A4 bon état",
        "Livre format 16",
        "Album taille 20 cm",
        "BD 48 pages couleur",
        # Années et éditions
        "Édition originale 1984",
        "Tirage spécial 2020",
        # Textes génériques
        "BD RIP bon état",
        "Collection bande dessinée",
    ],
)
def test_tome_extractor_false_positives_matrix(false_positive_title: str) -> None:
    """Matrice générale garantissant l'absence de faux positifs (prix, dimensions, années)."""
    res = TomeExtractor.extract(false_positive_title)
    # Ne doit jamais être classé comme un lot ni extraire un tome invalide
    assert res.is_lot is False
    assert res.tome_number is None


def test_tome_extractor_description_fallback():
    """Vérifie le repli sur la description si le titre ne mentionne pas le tome."""
    res = TomeExtractor.extract(
        title="BD RIP",
        description="Je vends le tome 3 en très bon état",
        series_key="RIP",
    )
    assert res.is_lot is False
    assert res.tome_number == 3
    assert res.confidence == "medium"


def test_tome_extractor_blacksad_hyphen_and_subtitles():
    """Vérifie la détection du Tome 6 avec tirets et ponctuation et du Tome 7."""
    res6 = TomeExtractor.extract(
        title="Blacksad -6- Alors, tout tombe - Première partie - Edition originale",
        series_key="Blacksad",
    )
    assert res6.is_lot is False
    assert res6.tome_number == 6
    assert res6.confidence == "high"

    res7 = TomeExtractor.extract(
        title="Blacksad Alors, tout tombe - Seconde partie",
        series_key="Blacksad",
    )
    assert res7.is_lot is False
    assert res7.tome_number == 7
    assert res7.confidence == "high"

    res_paren = TomeExtractor.extract(
        title="Blacksad (6)",
        series_key="Blacksad",
    )
    assert res_paren.is_lot is False
    assert res_paren.tome_number == 6

