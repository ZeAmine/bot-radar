"""Forme de l'hôte admise par le relais : nom accepté, adresse littérale refusée.

Le collecteur règle son proxy par une URL. Avec `socks5h://`, libcurl transmet le
nom d'hôte et laisse le proxy le résoudre ; avec `socks5://`, il résout lui-même
et transmet une adresse. Le relais durci refuse les adresses littérales : c'est
la raison du schéma `socks5h://`.

Ces tests portent sur `resolve_allowed_addresses`, là où la décision est prise.
Ils ne montrent **pas** ce que libcurl écrit sur la socket : cette moitié dépend
de `curl_cffi` et n'est pas vérifiée ici. La documentation opérationnelle cite la
documentation officielle de libcurl pour cette moitié, sans la présenter comme
un test.

Une adresse littérale est refusée par deux gardes indépendants : le test
d'adresse littérale, et la liste des domaines autorisés, dont une adresse n'est
jamais un sous-domaine. Neutraliser l'un laisse l'autre actif.
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from scripts.leboncoin_residential_relay import (
    domain_is_allowed,
    normalize_domain,
    resolve_allowed_addresses,
)
from tests.unit.test_leboncoin_residential_relay import _fake_resolution

ALLOWED = ("leboncoin.fr",)
# Adresse publique de documentation (TEST-NET relative à aucun hôte réel).
PUBLIQUE = "93.184.216.34"
# Adresse privée, pour prouver que la résolution vers le réseau local est refusée.
PRIVEE = "192.168.1.10"


@pytest.mark.parametrize(
    "adresse",
    ["93.184.216.34", "51.15.0.1", "8.8.8.8", "::1", "2001:4860:4860::8888"],
)
def test_a_literal_address_is_never_an_allowed_domain(
    monkeypatch: pytest.MonkeyPatch, adresse: str
):
    """Une adresse ne peut pas être le sous-domaine d'un domaine autorisé.

    Aucune résolution n'est nécessaire : le refus tombe sur la liste. Le double
    est posé quand même, pour qu'aucune résolution réelle ne puisse avoir lieu
    si l'implémentation changeait d'ordre.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolution(PUBLIQUE))

    assert domain_is_allowed(adresse, ALLOWED) is False
    assert resolve_allowed_addresses(adresse, 443, ALLOWED) is None


def test_an_allowed_domain_resolves(monkeypatch: pytest.MonkeyPatch):
    """La forme transmise par `socks5h://` est celle que le relais accepte."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolution(PUBLIQUE))

    resultat = resolve_allowed_addresses("www.leboncoin.fr", 443, ALLOWED)

    assert resultat


def test_a_resolved_allowed_domain_yields_only_global_addresses(
    monkeypatch: pytest.MonkeyPatch,
):
    """Un nom autorisé ne doit pas mener au réseau local du Pi.

    `is_global` est la propriété qu'applique le relais en dernier ressort ; elle
    couvre des cas que `is_private is False` laisserait passer (adresses
    réservées, de documentation, de benchmark).
    """
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolution(PUBLIQUE))

    for candidat in resolve_allowed_addresses("www.leboncoin.fr", 443, ALLOWED):
        assert ipaddress.ip_address(str(candidat[3][0])).is_global is True


def test_a_name_resolving_to_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
):
    """Un domaine autorisé qui résout vers le réseau local est refusé.

    C'est le cas qu'un domaine détourné produirait : la liste seule ne suffirait
    pas à empêcher le relais de servir de chemin vers le réseau du Pi.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolution(PRIVEE))

    assert resolve_allowed_addresses("www.leboncoin.fr", 443, ALLOWED) is None


def test_a_literal_address_is_refused_even_if_listed_as_a_domain(
    monkeypatch: pytest.MonkeyPatch,
):
    """Mettre une adresse dans l'allowlist ne la rend pas acceptable."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolution(PUBLIQUE))

    assert resolve_allowed_addresses("51.15.0.1", 443, ("51.15.0.1",)) is None


def test_a_non_allowed_domain_stays_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolution(PUBLIQUE))

    assert resolve_allowed_addresses("example.com", 443, ALLOWED) is None


def test_normalize_refuses_a_literal_address():
    """Une adresse ne peut pas entrer dans l'allowlist par la configuration."""
    with pytest.raises(ValueError, match="adresse IP littérale"):
        normalize_domain("93.184.216.34")
