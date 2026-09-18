"""Tests unitaires pour les fournisseurs de prix d'occasion.
Fournisseurs testés : eBay, Momox, Recyclivre, Gibert, Registry.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.providers.base import MarketOffer
from app.providers.ebay_browse import EbayBrowseProvider
from app.providers.gibert import GibertProvider
from app.providers.momox import MomoxProvider
from app.providers.recyclivre import RecyclivreProvider
from app.providers.registry import ProviderRegistry

# ==============================================================================
# TESTS EBAY BROWSE API
# ==============================================================================


@pytest.mark.asyncio
async def test_ebay_browse_valid_offer():
    """Vérifie le parsing d'une offre eBay valide avec prix et port connus."""
    provider = EbayBrowseProvider()
    settings.EBAY_CLIENT_ID = "dummy_id"
    settings.EBAY_CLIENT_SECRET = MagicMock(get_secret_value=lambda: "dummy_secret")

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
async def test_ebay_browse_reject_unknown_shipping_or_currency():
    """Vérifie le rejet strict des offres avec devise non-EUR ou port inconnu."""
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
                    "shippingOptions": [],  # Port inconnu -> Doit être rejeté
                },
                {
                    "title": "Mauvaise devise",
                    "price": {"value": "10.00", "currency": "USD"},  # Devise USD -> Rejeté
                    "shippingOptions": [{"shippingCost": {"value": "2.00", "currency": "USD"}}],
                },
            ]
        },
    )

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = search_response
        offers = await provider.get_offers(isbn13="9782205078054", title="Blacksad")
        assert len(offers) == 0


@pytest.mark.asyncio
async def test_ebay_browse_error_handling():
    """Vérifie le fallback silencieux en cas d'erreur de réseau ou OAuth."""
    provider = EbayBrowseProvider()
    with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectTimeout("Timeout")):
        offers = await provider.get_offers(isbn13="9782205078054", title="Blacksad")
        assert offers == []


# ==============================================================================
# TESTS MOMOX, RECYCLIVRE, GIBERT
# ==============================================================================


@pytest.mark.asyncio
async def test_momox_shipping_rules():
    """Vérifie la règle de livraison Momox (gratuite dès 19 €, sinon 2,99 €)."""
    provider = MomoxProvider()

    # Offre 1 : 10 € -> port 2,99 € -> 12,99 €
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

    # Offre 2 : 25 € -> port gratuit 0 € -> 25,00 €
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

    # Produit d'occasion disponible
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

    # Produit neuf uniquement -> Rejet
    res_new = httpx.Response(
        200,
        json={"products": [{"price": 15.00, "condition": "Neuf", "is_used": False}]},
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = res_new
        offers_new = await provider.get_offers(isbn13="9782205078054", title="Blacksad")
        assert len(offers_new) == 0


# ==============================================================================
# TESTS REGISTRY ET SÉLECTION UNIFIÉE
# ==============================================================================


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
        delivered_price_cents=1499,  # La plus basse !
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
