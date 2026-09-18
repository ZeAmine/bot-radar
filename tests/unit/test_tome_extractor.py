"""Tests unitaires pour le module d'extraction de tomes et de lots TomeExtractor."""

from app.services.tome_extractor import TomeExtractor


def test_tome_extractor_standard_arabic():
    """Vérifie la détection classique des tomes en chiffres arabes."""
    res1 = TomeExtractor.extract("BD RIP Tome 2", series_key="RIP")
    assert res1.is_lot is False
    assert res1.tome_number == 2
    assert res1.confidence == "high"

    res2 = TomeExtractor.extract("RIP T3", series_key="RIP")
    assert res2.is_lot is False
    assert res2.tome_number == 3

    res3 = TomeExtractor.extract("Blacksad vol. 4", series_key="Blacksad")
    assert res3.is_lot is False
    assert res3.tome_number == 4

    res4 = TomeExtractor.extract("Blast Volume 1", series_key="Blast")
    assert res4.is_lot is False
    assert res4.tome_number == 1

    res5 = TomeExtractor.extract("L'odyssée d'Hakim livre 3", series_key="L'odyssée d'Hakim")
    assert res5.is_lot is False
    assert res5.tome_number == 3

    res6 = TomeExtractor.extract("RIP #5", series_key="RIP")
    assert res6.is_lot is False
    assert res6.tome_number == 5


def test_tome_extractor_roman_numerals():
    """Vérifie la conversion des chiffres romains en numéros de tome."""
    res1 = TomeExtractor.extract("RIP Tome IV", series_key="RIP")
    assert res1.is_lot is False
    assert res1.tome_number == 4

    res2 = TomeExtractor.extract("Blacksad Tome II", series_key="Blacksad")
    assert res2.is_lot is False
    assert res2.tome_number == 2

    res3 = TomeExtractor.extract("Blast Tome VI", series_key="Blast")
    assert res3.is_lot is False
    assert res3.tome_number == 6


def test_tome_extractor_subtitles():
    """Vérifie la reconnaissance d'un tome via le sous-titre de l'album."""
    # Pour RIP : Maurice = Tome 2, Ahmed = Tome 3, Eugène = Tome 6
    res1 = TomeExtractor.extract("BD RIP - Maurice", series_key="RIP")
    assert res1.is_lot is False
    assert res1.tome_number == 2
    assert res1.matched_subtitle == "maurice"

    res2 = TomeExtractor.extract("RIP Ahmed", series_key="RIP")
    assert res2.is_lot is False
    assert res2.tome_number == 3

    # Pour Blacksad : Arctic-Nation = Tome 2
    res3 = TomeExtractor.extract("Blacksad Arctic-Nation", series_key="Blacksad")
    assert res3.is_lot is False
    assert res3.tome_number == 2

    # Pour Blast : Grasse Carcasse = Tome 1
    res4 = TomeExtractor.extract("Blast Grasse Carcasse", series_key="Blast")
    assert res4.is_lot is False
    assert res4.tome_number == 1


def test_tome_extractor_price_not_a_tome():
    """Vérifie qu'un prix en euros (ex: 15€, 20 euros) n'est jamais confondu avec un tome."""
    res1 = TomeExtractor.extract("BD RIP 15€", series_key="RIP")
    assert res1.tome_number is None

    res2 = TomeExtractor.extract("BD RIP 20 euros bon état", series_key="RIP")
    assert res2.tome_number is None

    res3 = TomeExtractor.extract("RIP T2 à 12€", series_key="RIP")
    assert res3.tome_number == 2


def test_tome_extractor_lot_detection():
    """Vérifie la détection de lots et de plages de tomes."""
    # Plage avec 'à'
    res1 = TomeExtractor.extract("Lot BD RIP tomes 1 à 4", series_key="RIP")
    assert res1.is_lot is True
    assert res1.lot_tomes == [1, 2, 3, 4]
    assert res1.lot_count == 4

    # Plage avec tiret
    res2 = TomeExtractor.extract("RIP tomes 1-3", series_key="RIP")
    assert res2.is_lot is True
    assert res2.lot_tomes == [1, 2, 3]

    # Énumération explicite
    res3 = TomeExtractor.extract("BD RIP T1 T2 T3", series_key="RIP")
    assert res3.is_lot is True
    assert res3.lot_tomes == [1, 2, 3]

    # Mot-clé lot avec nombre
    res4 = TomeExtractor.extract("Lot 3 BD RIP", series_key="RIP")
    assert res4.is_lot is True
    assert res4.lot_count == 3

    # Mot-clé intégrale ou coffret
    res5 = TomeExtractor.extract("Coffret RIP intégrale", series_key="RIP")
    assert res5.is_lot is True

    # Volumes explicites entre parenthèses ou suffixe
    res6 = TomeExtractor.extract("Slava (3 volumes) Dargaud", series_key="Slava")
    assert res6.is_lot is True
    assert res6.lot_count == 3


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


def test_tome_extractor_generic_fallback():
    """Vérifie qu'une annonce sans mention de tome renvoie None proprement."""
    res = TomeExtractor.extract("BD RIP bon état", series_key="RIP")
    assert res.is_lot is False
    assert res.tome_number is None
    assert res.confidence == "none"
