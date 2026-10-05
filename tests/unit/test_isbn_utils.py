"""Ce qu'un identifiant d'autorité porte, lu pour ce qu'il est.

Une source d'autorité n'écrit pas toujours le mot « ISBN » devant son code : un
EAN-13 en est un aussi, pour un livre. La lecture exigeait ce mot, si bien qu'une
notice entière disparaissait de la recherche sans laisser de trace.
"""

from __future__ import annotations

from app.utils.isbn import isbn_from_identifier


def test_bare_ean_is_read_as_an_isbn():
    """Le code nu d'un livre est son ISBN-13 : c'est le même code, pas un autre.

    9782812322495 est l'EAN affiché par une place de marché pour un livre de cuisine.
    """
    assert isbn_from_identifier("9782812322495") == "9782812322495"


def test_labelled_and_prefixed_forms_are_read_the_same_way():
    """Les trois écritures d'un même code rendent le même ISBN."""
    attendu = "9782812322495"

    assert isbn_from_identifier("urn:isbn:9782812322495") == attendu
    assert isbn_from_identifier("ISBN : 978-2-8123-2249-5") == attendu
    assert isbn_from_identifier("ISBN 9782812322495") == attendu


def test_isbn10_is_converted_to_isbn13():
    """Un ISBN-10 devient l'ISBN-13 correspondant : c'est l'identifiant stocké.

    La clé de contrôle est recalculée sur le préfixe 978, le « X » disparaît donc
    au passage : le code obtenu n'est pas l'ancien avec un chiffre devant.
    """
    assert isbn_from_identifier("ISBN 207040918X") == "9782070409181"


def test_ean_of_another_product_is_refused():
    """Un code-barres qui commence par 2 n'est pas un livre.

    Sa clé de contrôle est pourtant valide : c'est bien un code, mais il désigne
    autre chose, et l'attacher à une alerte ferait porter le mauvais identifiant.
    """
    assert isbn_from_identifier("2123456789010") is None


def test_invalid_checksum_is_refused():
    """Un code dont la clé de contrôle ne tombe pas est un code tronqué, pas un livre."""
    assert isbn_from_identifier("1234567890123") is None


def test_bnf_ark_url_is_not_an_isbn():
    """L'ark est une URL de notice : ses chiffres ne sont pas ceux d'un livre."""
    ark = "http://catalogue.bnf.fr/ark:/12148/cb471036825"

    assert isbn_from_identifier(ark) is None


def test_nothing_to_read():
    assert isbn_from_identifier(None) is None
    assert isbn_from_identifier("") is None
    assert isbn_from_identifier("sans code") is None
