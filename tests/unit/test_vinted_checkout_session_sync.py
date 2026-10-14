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


# ---------------------------------------------------------------------------
# Validation : un 200 ne suffit pas, le verdict se lit sur le corps
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: object, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def fake_httpx_client(monkeypatch: pytest.MonkeyPatch):
    """Remplace le client httpx du script : aucun appel réseau réel."""
    response_holder: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def get(self, _url: str, **kwargs: object) -> _FakeResponse:
            del kwargs  # le double n'inspecte pas les en-têtes
            return response_holder["response"]  # type: ignore[return-value]

    assert headers_are_used(monkeypatch, FakeClient)
    return response_holder


def headers_are_used(monkeypatch: pytest.MonkeyPatch, client_cls: type) -> bool:
    monkeypatch.setattr(sync.httpx, "Client", client_cls)
    return True


def test_validation_accepts_only_a_body_code_zero(
    fake_httpx_client: dict[str, object],
) -> None:
    fake_httpx_client["response"] = _FakeResponse(200, {"code": 0, "user": {"id": 1}})
    valid, reason = sync.validate_session({"access_token_web": "x"})
    assert valid is True
    assert "code=0" in reason


def test_validation_refuses_access_denied_even_with_http_200(
    fake_httpx_client: dict[str, object],
) -> None:
    """Le piège réel : la passerelle rend 200 avec un corps de refus."""
    fake_httpx_client["response"] = _FakeResponse(
        200, {"code": 106, "message": "Accès refusé", "message_code": "access_denied"}
    )
    valid, reason = sync.validate_session({"access_token_web": "x"})
    assert valid is False
    assert "identité confirmée" in reason or "refuse" in reason


def test_validation_refuses_a_login_required_body(
    fake_httpx_client: dict[str, object],
) -> None:
    fake_httpx_client["response"] = _FakeResponse(
        400, {"code": 21, "message_code": "user_login_required"}
    )
    valid, reason = sync.validate_session({"access_token_web": "x"})
    assert valid is False
    assert "HTTP 400" in reason


def test_validation_refuses_an_unreadable_body(
    fake_httpx_client: dict[str, object],
) -> None:
    fake_httpx_client["response"] = _FakeResponse(200, ValueError("no json"), text="<html>")
    valid, _ = sync.validate_session({"access_token_web": "x"})
    assert valid is False


def test_validation_reports_a_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> FailingClient:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def get(self, *_args: object, **_kwargs: object) -> object:
            raise sync.httpx.ConnectError("down")

    monkeypatch.setattr(sync.httpx, "Client", FailingClient)
    valid, reason = sync.validate_session({"access_token_web": "x"})
    assert valid is False
    assert "injoignable" in reason
