"""Tests unitaires du registre de prix d'occasion et de ses sélections."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.providers.base import (
    MarketOffer,
    extract_best_historical_low,
    normalize_search_isbn,
)
from app.providers.registry import ProviderRegistry


def test_extract_best_historical_low_empty():
    """Aucune offre ⇒ aucune valeur (pas de faux historique bas)."""
    assert extract_best_historical_low([]) is None


def test_extract_best_historical_low_ignores_missing_payloads():
    """Les offres sans payload ou sans donnée d'historique sont ignorées."""
    offers = [
        MarketOffer(
            provider_name="p1",
            item_price_cents=1000,
            shipping_price_cents=300,
            delivered_price_cents=1300,
            source_url="https://example.com/1",
            raw_payload=None,
        ),
        MarketOffer(
            provider_name="p2",
            item_price_cents=1200,
            shipping_price_cents=300,
            delivered_price_cents=1500,
            source_url="https://example.com/2",
            raw_payload={"other": 1},
        ),
    ]
    assert extract_best_historical_low(offers) is None


def test_extract_best_historical_low_returns_minimum():
    """Le plus bas historique certifié est retenu parmi les offres."""
    offers = [
        MarketOffer(
            provider_name="p1",
            item_price_cents=1000,
            shipping_price_cents=300,
            delivered_price_cents=1300,
            source_url="https://example.com/1",
            raw_payload={"historical_low_1y_cents": 2000},
        ),
        MarketOffer(
            provider_name="p2",
            item_price_cents=1200,
            shipping_price_cents=300,
            delivered_price_cents=1500,
            source_url="https://example.com/2",
            raw_payload={"historical_low_1y_cents": 900},
        ),
        MarketOffer(
            provider_name="p3",
            item_price_cents=1100,
            shipping_price_cents=300,
            delivered_price_cents=1400,
            source_url="https://example.com/3",
            raw_payload=None,
        ),
    ]
    assert extract_best_historical_low(offers) == 900


def test_registry_select_best_offer():
    """Vérifie la sélection de l'offre la moins chère au total livré."""
    offer_1 = MarketOffer(
        provider_name="marchand_1",
        item_price_cents=1500,
        shipping_price_cents=300,
        delivered_price_cents=1800,
        source_url="https://example.com/1",
    )
    offer_2 = MarketOffer(
        provider_name="marchand_2",
        item_price_cents=1200,
        shipping_price_cents=299,
        delivered_price_cents=1499,  # La plus basse.
        source_url="https://example.com/2",
    )
    offer_3 = MarketOffer(
        provider_name="marchand_3",
        item_price_cents=1300,
        shipping_price_cents=350,
        delivered_price_cents=1650,
        source_url="https://example.com/3",
    )

    best = ProviderRegistry.select_best_offer([offer_1, offer_2, offer_3])
    assert best is not None
    assert best.provider_name == "marchand_2"
    assert best.delivered_price_cents == 1499


def test_extract_best_historical_low_ignores_invalid_values():
    """Une valeur nulle, négative ou non numérique ne doit jamais casser le refresh."""

    def _with_payload(payload: dict[str, object] | None) -> MarketOffer:
        return MarketOffer(
            provider_name="p",
            item_price_cents=1000,
            shipping_price_cents=0,
            delivered_price_cents=1000,
            source_url="https://example.com",
            raw_payload=payload,
        )

    offers = [
        _with_payload({"historical_low_1y_cents": 0}),
        _with_payload({"historical_low_1y_cents": -500}),
        _with_payload({"historical_low_1y_cents": "abc"}),
        _with_payload({"historical_low_1y_cents": None}),
    ]
    assert extract_best_historical_low(offers) is None

    offers.append(_with_payload({"historical_low_1y_cents": 1250}))
    assert extract_best_historical_low(offers) == 1250


def test_normalize_search_isbn_requires_valid_checksum():
    """Un ISBN mal saisi est rejeté plutôt que propagé aux fournisseurs."""
    assert normalize_search_isbn("9782344045084") == "9782344045084"
    assert normalize_search_isbn("978-2-34404-508-4") == "9782344045084"
    assert normalize_search_isbn("9782365770000") is None  # chiffre de contrôle faux
    assert normalize_search_isbn("123") is None
    assert normalize_search_isbn(None) is None


@pytest.mark.asyncio
async def test_registry_never_queries_providers_with_invalid_isbn():
    """Le registre valide l'ISBN avant tout appel réseau."""
    fake_provider = MagicMock()
    fake_provider.name = "fake"
    fake_provider.get_offers = AsyncMock(return_value=[])

    registry = ProviderRegistry(providers=[fake_provider])
    await registry.fetch_all_offers(isbn13="9782365770000", title="Blacksad")

    assert fake_provider.get_offers.await_args.kwargs["isbn13"] is None

    await registry.fetch_all_offers(isbn13="9782344045084", title="Blacksad")
    assert fake_provider.get_offers.await_args.kwargs["isbn13"] == "9782344045084"


def test_registry_rejects_new_and_inferior_conditions():
    """Une offre en « Bon état » ou une annonce neuve ne peut pas gagner la cote."""

    def _offer(merchant: str, condition: str | None, delivered: int) -> MarketOffer:
        return MarketOffer(
            provider_name=merchant.lower(),
            item_price_cents=delivered,
            shipping_price_cents=0,
            delivered_price_cents=delivered,
            source_url="https://example.com",
            item_condition=condition,
        )

    offers = [
        _offer("recyclivre", "Bon état", 900),
        _offer("bd_fugue", "Neuf", 800),
        _offer("amazon_de", "Très bon", 700),
        _offer("rakuten", "Très bon", 1500),
    ]
    best = ProviderRegistry.select_best_offer(offers)
    assert best is not None
    assert best.provider_name == "rakuten"


def test_registry_same_merchant_twice_keeps_its_cheapest_offer():
    """Le même marchand gardé deux fois ne peut pas faire monter la cote."""
    cheap = MarketOffer(
        provider_name="chasse_aux_livres",
        item_price_cents=1499,
        shipping_price_cents=None,
        delivered_price_cents=1499,
        source_url="https://www.chasse-aux-livres.fr/prix/1",
        price_includes_shipping=True,
        item_condition="Très bon",
        raw_payload={"best_merchant": "Momox"},
    )
    expensive = MarketOffer(
        provider_name="chasse_aux_livres",
        item_price_cents=1800,
        shipping_price_cents=0,
        delivered_price_cents=1800,
        source_url="https://www.chasse-aux-livres.fr/prix/2",
        item_condition="Très bon",
        raw_payload={"best_merchant": "Momox"},
    )
    best = ProviderRegistry.select_best_offer([expensive, cheap])
    assert best is not None
    assert best.delivered_price_cents == 1499

