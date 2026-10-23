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


def test_un_timeout_generique_reste_de_phase_inconnue():
    """Un `Timeout` sans type précis ne dit pas si la connexion ou la réponse a expiré."""
    assert scraper.collection_phase(_FakeTimeout("peu importe")) == "inconnue"
    assert scraper.collection_phase(ValueError("peu importe")) == "inconnue"


def test_la_phase_ne_se_deduit_jamais_du_message():
    """Un message qui parle de proxy ne doit pas fabriquer une phase ProxyError."""
    classe = type("Timeout", (Exception,), {})
    assert scraper.collection_phase(classe("proxy connect failed")) == "inconnue"


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
    assert erreur.phase == "inconnue"
    assert erreur.exception_class.endswith("_FakeTimeout")
    assert erreur.duration_ms is not None
    assert erreur.duration_ms >= 0
    assert erreur.session_reused is False
    assert str(erreur) == "_FakeTimeout"

    enregistrements = [
        r for r in caplog.records if getattr(r, "event", "") == "leboncoin_collect_failed"
    ]
    assert len(enregistrements) == 1
    assert enregistrements[0].phase == "inconnue"
    assert "secret interne" not in caplog.text


def test_session_reutilisee_signalee(monkeypatch: pytest.MonkeyPatch):
    """Une session déjà ouverte pour ce proxy est signalée comme réutilisée."""
    monkeypatch.setattr(scraper, "_session", _session_qui_leve(ProxyError("x")))
    monkeypatch.setattr(scraper, "_session_proxy", "socks5h://exemple:1080")
    monkeypatch.setattr(
        scraper, "_get_session", lambda _p: _session_qui_leve(ProxyError("x"))
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


def test_la_ligne_reellement_ecrite_par_le_formateur_de_l_application(
    monkeypatch: pytest.MonkeyPatch,
):
    """Les `extra` ne valent que s'ils sortent du formateur que l'application installe.

    Ce test passe par `JSONFormatter`, celui de `setup_logging`, et lit la ligne
    produite : un champ présent dans l'enregistrement mais absent de la sortie
    serait invisible en production.
    """
    import io
    import json

    from app.core.logging import JSONFormatter

    flux = io.StringIO()
    gestionnaire = logging.StreamHandler(flux)
    gestionnaire.setFormatter(JSONFormatter())
    journal = logging.getLogger("radar.leboncoin")
    journal.addHandler(gestionnaire)
    journal.setLevel(logging.WARNING)
    monkeypatch.setattr(
        scraper, "_get_session", lambda _p: _session_qui_leve(_FakeTimeout("adresse interne"))
    )

    try:
        with pytest.raises(scraper.LeboncoinCollectionError):
            scraper.fetch_search_page("https://exemple/recherche", None, page=2)
    finally:
        journal.removeHandler(gestionnaire)

    lignes = [
        json.loads(ligne_brute)
        for ligne_brute in flux.getvalue().splitlines()
        if ligne_brute.strip()
    ]
    ecrit = [entree for entree in lignes if entree.get("event") == "leboncoin_collect_failed"]
    assert len(ecrit) == 1
    ligne = ecrit[0]

    assert ligne["exception_class"].endswith("_FakeTimeout")
    assert ligne["phase"] == "inconnue"
    assert isinstance(ligne["duration_ms"], int)
    assert ligne["session_reused"] is False
    assert ligne["page"] == 2
    assert "adresse interne" not in flux.getvalue()
