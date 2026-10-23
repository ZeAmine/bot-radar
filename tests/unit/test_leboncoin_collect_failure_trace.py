"""Échec de collecte : la phase et la durée sont journalisées, le message brut non.

Sans cette trace, un échec ne laisse que le nom de la classe levée par
`curl_cffi` (« Timeout »), qui ne dit pas si le proxy a refusé, si la connexion
a expiré ou si la réponse s'est fait attendre. Ces tests fixent ce que la trace
doit porter, et ce qu'elle ne doit jamais porter.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.services import leboncoin_scraper as scraper


class _FakeTimeout(Exception):  # noqa: N818
    """Nom de classe proche de celui de `curl_cffi`, module distinct."""


class _FakeProxy(Exception):  # noqa: N818
    """Erreur de proxy nommée comme celle de `curl_cffi`."""


@pytest.fixture(autouse=True)
def _session_neuve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scraper, "_session", None)
    monkeypatch.setattr(scraper, "_session_proxy", None)


def _session_qui_leve(exception: BaseException) -> Any:
    class _Session:
        def get(self, *_args: Any, **_kwargs: Any) -> Any:
            raise exception

    return _Session()


def test_phase_deduite_du_nom_de_classe():
    """La phase vient du type levé, jamais du texte du message."""
    assert scraper.collection_phase(_FakeTimeout("peu importe")) == "ReadTimeout"
    assert scraper.collection_phase(_FakeProxy("peu importe")) == "ProxyError"
    assert scraper.collection_phase(ValueError("peu importe")) == "autre"


class ConnectTimeout(Exception):  # noqa: N818
    """Nom exact reconnu sans heuristique."""


class ProxyError(Exception):  # noqa: N818
    """Nom exact reconnu sans heuristique."""


@pytest.mark.parametrize(
    ("exception", "phase"),
    [(ConnectTimeout, "ConnectTimeout"), (ProxyError, "ProxyError")],
)
def test_phases_explicites(exception: type[BaseException], phase: str):
    """Un nom exact n'est pas déduit d'une heuristique sur « Timeout »."""
    assert scraper.collection_phase(exception("x")) == phase


def test_echec_journalise_phase_duree_et_session(monkeypatch: pytest.MonkeyPatch, caplog):
    monkeypatch.setattr(
        scraper, "_get_session", lambda _p: _session_qui_leve(_FakeTimeout("secret interne"))
    )

    contexte = caplog.at_level(logging.WARNING, logger="radar.leboncoin")
    with contexte, pytest.raises(scraper.LeboncoinCollectionError) as captured:
        scraper.fetch_search_page("https://exemple/recherche", None)

    erreur = captured.value
    assert erreur.phase == "ReadTimeout"
    assert erreur.exception_class.endswith("_FakeTimeout")
    assert erreur.duration_ms is not None
    assert erreur.duration_ms >= 0
    assert erreur.session_reused is False
    assert str(erreur) == "_FakeTimeout"

    enregistrements = [
        r for r in caplog.records if getattr(r, "event", "") == "leboncoin_collect_failed"
    ]
    assert len(enregistrements) == 1
    assert enregistrements[0].phase == "ReadTimeout"
    assert "secret interne" not in caplog.text


def test_session_reutilisee_signalee(monkeypatch: pytest.MonkeyPatch):
    """Une session déjà ouverte pour ce proxy est signalée comme réutilisée."""
    monkeypatch.setattr(scraper, "_session", _session_qui_leve(_FakeProxy("x")))
    monkeypatch.setattr(scraper, "_session_proxy", "socks5h://exemple:1080")
    monkeypatch.setattr(
        scraper, "_get_session", lambda _p: _session_qui_leve(_FakeProxy("x"))
    )

    with pytest.raises(scraper.LeboncoinCollectionError) as captured:
        scraper.fetch_search_page("https://exemple/recherche", "socks5h://exemple:1080")

    assert captured.value.session_reused is True
    assert captured.value.phase == "ProxyError"


def test_message_metier_inchange_pour_un_statut_http(monkeypatch: pytest.MonkeyPatch):
    """Les statuts non bloquants gardent leur message d'origine."""

    class _Reponse:
        status_code = 500
        text = "erreur"

    class _Session:
        def get(self, *_args: Any, **_kwargs: Any) -> Any:
            return _Reponse()

    monkeypatch.setattr(scraper, "_get_session", lambda _p: _Session())

    with pytest.raises(scraper.LeboncoinCollectionError) as captured:
        scraper.fetch_search_page("https://exemple/recherche", None)

    assert str(captured.value) == "HTTP 500"
    assert captured.value.exception_class is None
