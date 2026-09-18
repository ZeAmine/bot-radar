"""Tests unitaires pour le fournisseur de prix agrégés Chasse-aux-livres."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.core.constants import MatchConfidence
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider

MOCK_SEARCH_PAGE_HTML = """
<!DOCTYPE html>
<html lang="fr-FR">
<head><title>Recherche</title></head>
<body>
<div id="hash-cont" data-ajax="true" data-hash="mockhash1234567890abcdef12345678" style="display: none"></div>
</body>
</html>
"""

MOCK_REST_JSON = {
    "b4": True,
    "clear": False,
    "d": """
    <tr id="p1-0" data-indx=1000 data-relv=10000 data-pubd="20180914" data-lpric="1790" data-pag="112">
      <td class="cover-column">
        <div class="cover-column-img">
          <div class="favoriteImg" data-id="B07B5VXYBF" data-sig="mock">
            <a href="/prix/B07B5VXYBF/rip-tome-1-julien-monier-9791095670568" title="Rip Tome 1">
              <img src="https://img.chasse-aux-livres.fr/cover.jpg"/>
            </a>
          </div>
        </div>
      </td>
      <td class="results-book-details">
        <div class="title">
          <a href="/prix/B07B5VXYBF/rip-tome-1-julien-monier-9791095670568">Rip Tome 1</a>
        </div>
        <div class="subtitle">
          <a href="/prix/B07B5VXYBF/rip-tome-1-julien-monier-9791095670568">Derrick - Je Ne Survivrai Pas</a>
        </div>
        <div class="book-details3">
          <div class="binding">ISBN&nbsp;: 9791095670568</div>
        </div>
      </td>
      <td class="compare-box">
        <a href="/prix/B07B5VXYBF/rip-tome-1-julien-monier-9791095670568" class="btn">Comparer</a>
      </td>
    </tr>
    """,
}

MOCK_PROD_HTML = (
    '<div id="book-details" data-asin="B07B5VXYBF" data-lvs="mocklvs" data-nbeng="5"></div>'
)

MOCK_LOOKUP_JSON = {
    "offers": {
        "0": [
            {
                "logo": {"name": "MOMOX_SHOP"},
                "condition": {"_name": "USED"},
                "subCondition": {"name": "Très bon"},
                "totalPrice": {"amount": 1499, "formattedPrice": "14,99 €"},
                "affiliateUrl": "https://example.com/momox",
            },
            {
                "logo": {"name": "RAKUTEN"},
                "condition": {"_name": "USED"},
                "subCondition": {"name": "Comme neuf"},
                "totalPrice": {"amount": 1790, "formattedPrice": "17,90 €"},
                "affiliateUrl": "https://example.com/rakuten",
            },
        ]
    }
}


@pytest.mark.asyncio
async def test_chasse_aux_livres_success_by_title():
    provider = ChasseAuxLivresProvider()

    resp_search_page = httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
    resp_rest = httpx.Response(200, json=MOCK_REST_JSON)
    resp_prod = httpx.Response(200, text=MOCK_PROD_HTML)
    resp_lookup = httpx.Response(200, json=MOCK_LOOKUP_JSON)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [resp_search_page, resp_rest, resp_prod, resp_lookup]

        offers = await provider.get_offers(isbn13=None, title="Rip Tome 1")
        assert len(offers) == 1
        offer = offers[0]

        assert offer.provider_name == "chasse_aux_livres"
        assert offer.delivered_price_cents == 1499
        assert offer.item_price_cents == 1499
        assert offer.shipping_price_cents == 0
        assert "example.com/momox" in offer.source_url
        assert offer.confidence == MatchConfidence.HIGH
        assert offer.is_available is True
        assert offer.is_comparable is True
        assert offer.raw_payload is not None
        assert offer.raw_payload.get("isbn13") == "9791095670568"
        assert "Momox" in offer.item_condition


@pytest.mark.asyncio
async def test_chasse_aux_livres_success_by_isbn():
    provider = ChasseAuxLivresProvider()

    resp_search_page = httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
    resp_rest = httpx.Response(200, json=MOCK_REST_JSON)
    resp_prod = httpx.Response(200, text=MOCK_PROD_HTML)
    resp_lookup = httpx.Response(200, json=MOCK_LOOKUP_JSON)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [resp_search_page, resp_rest, resp_prod, resp_lookup]

        offers = await provider.get_offers(isbn13="9791095670568", title="Rip")
        assert len(offers) == 1
        assert offers[0].delivered_price_cents == 1499
        assert "Momox" in offers[0].item_condition


@pytest.mark.asyncio
async def test_chasse_aux_livres_no_hash():
    provider = ChasseAuxLivresProvider()
    resp_search_page = httpx.Response(200, text="<html><body>No hash here</body></html>")

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = resp_search_page

        offers = await provider.get_offers(isbn13=None, title="Inconnu")
        assert len(offers) == 0


@pytest.mark.asyncio
async def test_chasse_aux_livres_network_error():
    provider = ChasseAuxLivresProvider()

    with patch("httpx.AsyncClient.get", side_effect=httpx.ConnectTimeout("Timeout")):
        offers = await provider.get_offers(isbn13=None, title="Rip")
        assert len(offers) == 0


@pytest.mark.asyncio
async def test_chasse_aux_livres_empty_query():
    provider = ChasseAuxLivresProvider()
    offers = await provider.get_offers(isbn13=None, title="")
    assert len(offers) == 0


@pytest.mark.asyncio
async def test_chasse_aux_livres_deep_lookup_success():
    provider = ChasseAuxLivresProvider()

    resp_search = httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
    resp_rest = httpx.Response(200, json=MOCK_REST_JSON)
    resp_prod = httpx.Response(200, text=MOCK_PROD_HTML)
    resp_lookup = httpx.Response(200, json=MOCK_LOOKUP_JSON)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [resp_search, resp_rest, resp_prod, resp_lookup]

        offers = await provider.get_offers(isbn13="9791095670568", title="Rip - Tome 1")
        assert len(offers) == 1
        offer = offers[0]
        assert offer.delivered_price_cents == 1499
        assert "Momox" in offer.item_condition
        assert offer.raw_payload is not None
        assert "partners" in offer.raw_payload
        assert "Momox" in offer.raw_payload["partners"]
        assert "Rakuten" in offer.raw_payload["partners"]


@pytest.mark.asyncio
async def test_chasse_aux_livres_title_mismatch_rejected():
    """Vérifie qu'un résultat dont le titre est disjoint de la recherche est rejeté (ex: Ogrest pour Loba Loca)."""
    provider = ChasseAuxLivresProvider()

    resp_search = httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
    # MOCK_REST_JSON renvoie 'Rip Tome 1'
    resp_rest = httpx.Response(200, json=MOCK_REST_JSON)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [resp_search, resp_rest]

        # Recherche pour "Mutafukaz Loba Loca" avec réponse "Rip Tome 1"
        offers = await provider.get_offers(isbn13="9791033511908", title="Mutafukaz Loba Loca")
        assert len(offers) == 0


@pytest.mark.asyncio
async def test_chasse_aux_livres_strict_used_filtering():
    """Vérifie que si seules des offres neuves sont retournées, aucune offre d'occasion n'est inventée."""
    provider = ChasseAuxLivresProvider()

    resp_search = httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
    resp_rest = httpx.Response(200, json=MOCK_REST_JSON)
    resp_prod = httpx.Response(200, text=MOCK_PROD_HTML)
    mock_lookup_only_new = {
        "offers": {
            "0": [
                {
                    "logo": {"name": "BD_FUGUE"},
                    "condition": {"_name": "NEW"},
                    "subCondition": {"name": "Neuf"},
                    "totalPrice": {"amount": 3500, "formattedPrice": "35,00 €"},
                    "affiliateUrl": "https://example.com/bdfugue",
                }
            ]
        }
    }
    resp_lookup = httpx.Response(200, json=mock_lookup_only_new)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [resp_search, resp_rest, resp_prod, resp_lookup]

        offers = await provider.get_offers(isbn13="9791095670568", title="Rip Tome 1")
        assert len(offers) == 0
