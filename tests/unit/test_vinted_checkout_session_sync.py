"""La lecture des cookies du script de session checkout : formats et refus.

Le script ne touche jamais Vinted dans ces tests : seule la lecture de l'entrée
est exercée, et les valeurs ne sont jamais journalisées.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import sync_vinted_checkout_session as sync  # noqa: E402


def test_cookie_header_is_parsed_without_values_leaking() -> None:
    cookies = sync.parse_cookie_input("access_token_web=abc; _vinted_fr_session=def; anon_id=42")
    assert cookies == {
        "access_token_web": "abc",
        "_vinted_fr_session": "def",
        "anon_id": "42",
    }


def test_prefixed_cookie_header_is_accepted() -> None:
    cookies = sync.parse_cookie_input("cookie: access_token_web=abc; anon_id=42")
    assert cookies == {"access_token_web": "abc", "anon_id": "42"}


def test_json_object_is_accepted() -> None:
    cookies = sync.parse_cookie_input('{"access_token_web": "abc", "vide": ""}')
    assert cookies == {"access_token_web": "abc"}


def test_broken_json_is_refused() -> None:
    assert sync.parse_cookie_input('{"access_token_web": ') == {}


def test_json_array_is_refused() -> None:
    assert sync.parse_cookie_input('["access_token_web=abc"]') == {}


@pytest.mark.parametrize("raw", ["", "   ", "pas_de_signe_egal"])
def test_unusable_input_yields_nothing(raw: str) -> None:
    assert sync.parse_cookie_input(raw) == {}
