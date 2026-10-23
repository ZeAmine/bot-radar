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

import pytest

from scripts.leboncoin_residential_relay import (
    domain_is_allowed,
    normalize_domain,
    resolve_allowed_addresses,
)

ALLOWED = ("leboncoin.fr",)


@pytest.mark.parametrize(
    "adresse",
    ["93.184.216.34", "51.15.0.1", "8.8.8.8", "::1", "2001:4860:4860::8888"],
)
def test_a_literal_address_is_never_an_allowed_domain(adresse: str):
    """Une adresse ne peut pas être le sous-domaine d'un domaine autorisé."""
    assert domain_is_allowed(adresse, ALLOWED) is False
    assert resolve_allowed_addresses(adresse, 443, ALLOWED) is None


def test_an_allowed_domain_resolves():
    """La forme transmise par `socks5h://` est celle que le relais accepte."""
    resultat = resolve_allowed_addresses("www.leboncoin.fr", 443, ALLOWED)

    assert resultat


def test_a_resolved_allowed_domain_yields_only_public_addresses():
    """Un nom autorisé ne doit pas mener au réseau local du Pi."""
    for candidat in resolve_allowed_addresses("www.leboncoin.fr", 443, ALLOWED):
        assert ipaddress.ip_address(str(candidat[3][0])).is_private is False


def test_a_literal_address_is_refused_even_if_listed_as_a_domain():
    """Mettre une adresse dans l'allowlist ne la rend pas acceptable."""
    assert resolve_allowed_addresses("51.15.0.1", 443, ("51.15.0.1",)) is None


def test_a_non_allowed_domain_stays_refused():
    assert resolve_allowed_addresses("example.com", 443, ALLOWED) is None


def test_normalize_refuses_a_literal_address():
    """Une adresse ne peut pas entrer dans l'allowlist par la configuration."""
    with pytest.raises(ValueError, match="adresse IP littérale"):
        normalize_domain("93.184.216.34")
