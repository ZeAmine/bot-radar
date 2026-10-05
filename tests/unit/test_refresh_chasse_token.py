"""L'extraction du jeton `duih`, testée sans navigateur ni réseau.

La capture Playwright elle-même ne se teste pas ici : elle demande un navigateur
et un défi réel. La règle de sélection entre attribut HTML et cookie, elle, est
pure — et c'est elle qui décide si le script renvoie un jeton de défi résolu ou
un cookie d'entrée sans valeur.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from refresh_chasse_token import extract_duih  # noqa: E402


def _cookie(name: str, value: str) -> dict:
    return {"name": name, "value": value}


def test_the_html_attribute_wins_over_the_cookie():
    """Un cookie `duih` existe dès l'entrée : seul l'attribut signale le défi passé."""
    token = extract_duih(
        "jeton-du-defi",
        [_cookie("duih", "cookie-davant")],
    )

    assert token == "jeton-du-defi"


def test_the_cookie_is_the_fallback_when_the_attribute_is_absent():
    """Le site peut aussi ne rendre l'attribut qu'après coup : le cookie suffit."""
    token = extract_duih(None, [_cookie("duih", "jeton-du-cookie")])

    assert token == "jeton-du-cookie"


def test_an_empty_attribute_falls_back_to_the_cookie():
    """Une chaîne blanche n'est pas un jeton : elle ne doit pas écraser le cookie."""
    token = extract_duih("   ", [_cookie("duih", "jeton-du-cookie")])

    assert token == "jeton-du-cookie"


def test_nothing_found_is_none_not_empty_string():
    """Le script distingue « pas encore » de « trouvé » par un None explicite."""
    assert extract_duih(None, [_cookie("session", "autre")]) is None
    assert extract_duih(None, []) is None
