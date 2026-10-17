"""Tests unitaires pour les fournisseurs de prix d'occasion.
Fournisseurs testés : eBay, Momox, Recyclivre, Gibert, Registry.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.providers.base import (
    MarketOffer,
    extract_best_historical_low,
    normalize_search_isbn,
)
from app.providers.ebay_browse import EbayBrowseProvider
from app.providers.gibert import GibertProvider
from app.providers.momox import MomoxProvider
from app.providers.recyclivre import RecyclivreProvider
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


@pytest.mark.asyncio
async def test_ebay_browse_valid_offer(monkeypatch: pytest.MonkeyPatch):
    """Vérifie le parsing d'une offre eBay valide avec prix et port connus."""
    provider = EbayBrowseProvider()
    monkeypatch.setattr(settings, "EBAY_CLIENT_ID", "dummy_id")
    monkeypatch.setattr(
        settings, "EBAY_CLIENT_SECRET", MagicMock(get_secret_value=lambda: "dummy_secret")
    )

    oauth_response = httpx.Response(200, json={"access_token": "mock_token", "expires_in": 7200})
    search_response = httpx.Response(
        200,
        json={
            "itemSummaries": [
                {
                    "itemId": "v1|123|0",
                    "title": "Blacksad Integrale",
                    "price": {"value": "15.00", "currency": "EUR"},
                    "shippingOptions": [{"shippingCost": {"value": "4.50", "currency": "EUR"}}],
                    "itemWebUrl": "https://www.ebay.fr/itm/123",
                    "condition": "Très bon état",
                }
            ]
        },
    )

    with (
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
    ):
        mock_post.return_value = oauth_response
        mock_get.return_value = search_response

        offers = await provider.get_offers(isbn13="9782205078054", title="Blacksad")
        assert len(offers) == 1
        offer = offers[0]
        assert offer.provider_name == "ebay"
        assert offer.item_price_cents == 1500
        assert offer.shipping_price_cents == 450
        assert offer.delivered_price_cents == 1950  # 1500 + 450
        assert offer.source_url == "https://www.ebay.fr/itm/123"


@pytest.mark.asyncio
async def test_ebay_browse_reject_unknown_shipping_or_currency(
    monkeypatch: pytest.MonkeyPatch,
):
    """Vérifie le rejet strict des offres avec devise non-EUR ou port inconnu."""
    monkeypatch.setattr(settings, "EBAY_CLIENT_ID", "dummy_id")
    monkeypatch.setattr(
        settings, "EBAY_CLIENT_SECRET", MagicMock(get_secret_value=lambda: "dummy_secret")
    )
    provider = EbayBrowseProvider()
    provider._cached_token = "valid_token"
    provider._token_expires_at = 9999999999.0

    search_response = httpx.Response(
        200,
        json={
            "itemSummaries": [
                {
                    "title": "Sans port",
                    "price": {"value": "10.00", "currency": "EUR"},
                    "shippingOptions": [],
                },
                {
                    "title": "Mauvaise devise",
                    "price": {"value": "10.00", "currency": "USD"},
                    "shippingOptions": [{"shippingCost": {"value": "2.00", "currency": "USD"}}],
                },
            ]
        },
    )

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = search_response
        offers = await provider.get_offers(isbn13="9782205078054", title="Blacksad")
        # La requête a bien eu lieu : sans identifiants, `get_offers` rend [] au
        # premier garde, et le rejet qu'on veut prouver ne serait pas exercé.
        mock_get.assert_called_once()
        assert len(offers) == 0


@pytest.mark.asyncio
async def test_ebay_browse_error_handling(monkeypatch: pytest.MonkeyPatch):
    """Vérifie le fallback silencieux en cas d'erreur de réseau ou OAuth."""
    monkeypatch.setattr(settings, "EBAY_CLIENT_ID", "dummy_id")
    monkeypatch.setattr(
        settings, "EBAY_CLIENT_SECRET", MagicMock(get_secret_value=lambda: "dummy_secret")
    )
    provider = EbayBrowseProvider()
    with patch(
        "httpx.AsyncClient.post",
        new_callable=AsyncMock,
        side_effect=httpx.ConnectTimeout("Timeout"),
    ) as mock_post:
        offers = await provider.get_offers(isbn13="9782205078054", title="Blacksad")
        # Le OAuth a bien été tenté : sans identifiants, le premier garde rend []
        # sans jamais joindre le réseau, et le fallback ne serait pas exercé.
        mock_post.assert_called_once()
        assert offers == []


@pytest.mark.asyncio
async def test_momox_shipping_rules():
    """Vérifie la règle de livraison Momox (gratuite dès 19 €, sinon 2,99 €)."""
    provider = MomoxProvider()

    res_under = httpx.Response(
        200,
        json={
            "data": [
                {"attributes": {"price": 10.0, "condition": "Très bon état", "slug": "item-1"}}
            ]
        },
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = res_under
        offers_under = await provider.get_offers(isbn13="9782205078054", title="Blacksad")
        assert len(offers_under) == 1
        assert offers_under[0].item_price_cents == 1000
        assert offers_under[0].shipping_price_cents == 299
        assert offers_under[0].delivered_price_cents == 1299

    res_over = httpx.Response(
        200,
        json={
            "data": [
                {"attributes": {"price": 25.0, "condition": "Très bon état", "slug": "item-2"}}
            ]
        },
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = res_over
        offers_over = await provider.get_offers(isbn13="9782205078054", title="Blacksad")
        assert len(offers_over) == 1
        assert offers_over[0].item_price_cents == 2500
        assert offers_over[0].shipping_price_cents == 0
        assert offers_over[0].delivered_price_cents == 2500


@pytest.mark.asyncio
async def test_recyclivre_standard_shipping():
    """Vérifie la récupération de la première offre Recyclivre et l'ajout du port unitaire."""
    provider = RecyclivreProvider()
    res = httpx.Response(
        200,
        json={
            "products": [
                {"unit_price": 7.50, "condition": "Bon état", "url": "https://recyclivre.com/123"}
            ]
        },
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = res
        offers = await provider.get_offers(isbn13="9782205078054", title="Blacksad")
        assert len(offers) == 1
        assert offers[0].item_price_cents == 750
        assert offers[0].shipping_price_cents == 299
        assert offers[0].delivered_price_cents == 1049


@pytest.mark.asyncio
async def test_gibert_used_only_filtering():
    """Vérifie que Gibert ne retient que les offres d'occasion."""
    provider = GibertProvider()

    res_used = httpx.Response(
        200,
        json={"products": [{"used_price": 8.00, "url": "https://gibert.com/123"}]},
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = res_used
        offers = await provider.get_offers(isbn13="9782205078054", title="Blacksad")
        assert len(offers) == 1
        assert offers[0].item_price_cents == 800
        assert offers[0].shipping_price_cents == 350
        assert offers[0].delivered_price_cents == 1150

    res_new = httpx.Response(
        200,
        json={"products": [{"price": 15.00, "condition": "Neuf", "is_used": False}]},
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = res_new
        offers_new = await provider.get_offers(isbn13="9782205078054", title="Blacksad")
        assert len(offers_new) == 0


def test_registry_select_best_offer():
    """Vérifie la sélection de l'offre la moins chère au total livré."""
    offer_ebay = MarketOffer(
        provider_name="ebay",
        item_price_cents=1500,
        shipping_price_cents=300,
        delivered_price_cents=1800,
        source_url="https://ebay.fr",
    )
    offer_momox = MarketOffer(
        provider_name="momox",
        item_price_cents=1200,
        shipping_price_cents=299,
        delivered_price_cents=1499,  # La plus basse.
        source_url="https://momox.fr",
    )
    offer_gibert = MarketOffer(
        provider_name="gibert",
        item_price_cents=1300,
        shipping_price_cents=350,
        delivered_price_cents=1650,
        source_url="https://gibert.fr",
    )

    best = ProviderRegistry.select_best_offer([offer_ebay, offer_momox, offer_gibert])
    assert best is not None
    assert best.provider_name == "momox"
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


def test_registry_deduplicates_same_merchant():
    """Le même marchand remonté deux fois ne compte qu'une fois, à son meilleur prix."""
    cheap = MarketOffer(
        provider_name="chasse_aux_livres",
        item_price_cents=1499,
        shipping_price_cents=None,
        delivered_price_cents=1499,
        source_url="https://momox.fr/cheap",
        price_includes_shipping=True,
        item_condition="Très bon",
        raw_payload={"best_merchant": "Momox"},
    )
    expensive = MarketOffer(
        provider_name="momox",
        item_price_cents=1800,
        shipping_price_cents=0,
        delivered_price_cents=1800,
        source_url="https://momox.fr/expensive",
        item_condition="Très bon",
        raw_payload={"best_merchant": "Momox"},
    )
    best = ProviderRegistry.select_best_offer([expensive, cheap])
    assert best is not None
    assert best.delivered_price_cents == 1499

