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
<div id="hash-cont" data-ajax="true"
data-hash="mockhash1234567890abcdef12345678" style="display: none"></div>
</body>
</html>
"""

MOCK_REST_JSON = {
    "b4": True,
    "clear": False,
    "d": """
    <tr id="p1-0" data-indx=1000 data-relv=10000
    data-pubd="20180914" data-lpric="1790" data-pag="112">
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
          <a href="/prix/B07B5VXYBF/rip-tome-1-julien-monier-9791095670568"
          >Derrick - Je Ne Survivrai Pas</a>
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


def _recording_provider() -> tuple[ChasseAuxLivresProvider, list[float]]:
    """Provider dont le backoff est enregistré au lieu d'être attendu.

    Sans cela, un test qui atteint la boucle de retry paie 1,5 s puis 1,5 s en
    temps réel. La liste renvoyée permet surtout d'affirmer qu'aucun repli n'a
    eu lieu : un `side_effect` épuisé lève, l'exception est avalée, `get_offers`
    renvoie `[]` — et un test qui n'assert qu'une liste vide passe alors pour la
    mauvaise raison.
    """
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    return ChasseAuxLivresProvider(sleep=fake_sleep), slept


def _search_call_count(mock_get: AsyncMock) -> int:
    """Nombre de pages de recherche demandées : une par tentative de `get_offers`.

    Compte les requêtes et non les appels totaux : le nombre exact d'appels
    profonds est un détail d'implémentation, les deux tentatives (ISBN puis
    titre) sont ce que le test doit voir.
    """
    return sum(1 for call in mock_get.call_args_list if "/search?query=" in call.args[0])


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
        assert offer.shipping_price_cents is None
        assert offer.price_includes_shipping is True
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
    provider, slept = _recording_provider()

    with patch("httpx.AsyncClient.get", side_effect=httpx.ConnectTimeout("Timeout")):
        offers = await provider.get_offers(isbn13=None, title="Rip")

    assert len(offers) == 0
    # Retry vérifié sans attente réelle : deux pauses de 1,5 s, une par échec.
    assert slept == [1.5, 1.5]


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
    """Vérifie qu'un résultat dont le titre est disjoint de la recherche
    est rejeté (ex: Ogrest pour Loba Loca)."""
    provider, slept = _recording_provider()

    resp_search = httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
    resp_rest = httpx.Response(200, json=MOCK_REST_JSON)
    resp_prod = httpx.Response(200, text=MOCK_PROD_HTML)
    resp_lookup = httpx.Response(200, json=MOCK_LOOKUP_JSON)

    # Deux passes (ISBN, titre) × quatre réponses : un `side_effect` trop court
    # serait avalé par le retry et ferait passer le test pour la mauvaise raison.
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [
            resp_search,
            resp_rest,
            resp_prod,
            resp_lookup,
            resp_search,
            resp_rest,
            resp_prod,
            resp_lookup,
        ]

        offers = await provider.get_offers(isbn13="9791033511908", title="Mutafukaz Loba Loca")

    assert len(offers) == 0
    # Les deux recherches ont bien abouti, sans repli silencieux.
    assert _search_call_count(mock_get) == 2
    assert slept == []


@pytest.mark.asyncio
async def test_isbn_exact_prime_sur_un_titre_incoherent():
    """L'ISBN exact rend le titre inutile : l'identité est déjà prouvée (ADR-035).

    Réalité constatée : le site orthographie « 1629, ou l'effrayante histoire
    des naufragés du Jakarta » là où l'alerte demande « Les naufragés du
    Jakarta T1 » — similarité 0,59 sous le seuil 0,72, et la cote ne partait
    jamais alors que le bon livre était sous nos yeux.
    """
    provider, slept = _recording_provider()

    resp_search = httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
    # MOCK_REST_JSON affiche l'ISBN cherché mais un titre sans rapport.
    resp_rest = httpx.Response(200, json=MOCK_REST_JSON)
    resp_prod = httpx.Response(200, text=MOCK_PROD_HTML)
    resp_lookup = httpx.Response(200, json=MOCK_LOOKUP_JSON)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [resp_search, resp_rest, resp_prod, resp_lookup]

        offers = await provider.get_offers(
            isbn13="9791095670568", title="Les naufragés du Jakarta T1"
        )

    assert len(offers) == 1
    assert offers[0].delivered_price_cents == 1499
    # La preuve suffit du premier coup : aucun repli par titre nécessaire.
    assert _search_call_count(mock_get) == 1
    assert slept == []


@pytest.mark.asyncio
async def test_titre_incoherent_rejette_sans_preuve_isbn():
    """Sans correspondance d'ISBN, le veto de titre reste strictement en place.

    Le filet d'ADR-016 ne doit pas disparaître derrière la preuve ISBN :
    un résultat affichant un AUTRE code que celui cherché reste rejeté,
    y compris sur le repli par titre.
    """
    provider, slept = _recording_provider()

    resp_search = httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
    resp_rest = httpx.Response(200, json=MOCK_REST_JSON)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        # ISBN recherché valide mais différent de l'affiché : le veto tombe avant
        # la fiche produit, donc aucune réponse produit/lookup n'est consommée.
        mock_get.side_effect = [resp_search, resp_rest, resp_search, resp_rest]

        offers = await provider.get_offers(
            isbn13="9782344045084", title="Les naufragés du Jakarta T1"
        )

    assert offers == []
    # Recherche ISBN puis repli titre : les deux atteignent le veto, sans réponse manquante.
    assert _search_call_count(mock_get) == 2
    assert mock_get.call_count == 4
    assert slept == []


@pytest.mark.asyncio
async def test_repli_par_titre_accepte_sur_preuve_isbn():
    """Le repli par titre profite aussi de la preuve ISBN (ADR-035).

    La recherche par ISBN peut tomber sur un résultat vide ; le repli par
    titre retrouve la même fiche, dont l'ISBN affiché coïncide avec celui
    de l'alerte — l'identité est prouvée même si la graphie diverge.
    """
    provider, slept = _recording_provider()

    resp_search = httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
    resp_rest_empty = httpx.Response(200, json={"d": ""})
    resp_rest = httpx.Response(200, json=MOCK_REST_JSON)
    resp_prod = httpx.Response(200, text=MOCK_PROD_HTML)
    resp_lookup = httpx.Response(200, json=MOCK_LOOKUP_JSON)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [
            resp_search,
            resp_rest_empty,
            resp_search,
            resp_rest,
            resp_prod,
            resp_lookup,
        ]

        offers = await provider.get_offers(
            isbn13="9791095670568", title="Les naufragés du Jakarta T1"
        )

    assert len(offers) == 1
    assert offers[0].delivered_price_cents == 1499
    assert _search_call_count(mock_get) == 2
    assert slept == []


@pytest.mark.asyncio
async def test_chasse_aux_livres_strict_used_filtering():
    """Vérifie que si seules des offres neuves sont retournées,
    aucune offre d'occasion n'est inventée."""
    provider, slept = _recording_provider()

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
        mock_get.side_effect = [
            resp_search, resp_rest, resp_prod, resp_lookup, resp_lookup,
        ]

        offers = await provider.get_offers(isbn13="9791095670568", title="Rip Tome 1")

    assert len(offers) == 0
    assert _search_call_count(mock_get) == 1
    assert mock_get.await_count == 5
    assert offers.attempts[0].status == "partial"
    assert offers.attempts[0].reason == "incomplete_offers"
    assert slept == []


@pytest.mark.asyncio
async def test_chasse_aux_livres_excludes_amazon_de():
    """Vérifie que les offres Amazon (De) sont strictement ignorées
    au profit des marchands francophones."""
    provider = ChasseAuxLivresProvider()

    resp_search = httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
    resp_rest = httpx.Response(200, json=MOCK_REST_JSON)
    resp_prod = httpx.Response(200, text=MOCK_PROD_HTML)
    mock_lookup_with_amazon_de = {
        "offers": {
            "0": [
                {
                    "logo": {"name": "AMAZON_DE"},
                    "condition": {"_name": "USED"},
                    "subCondition": {"name": "Très bon"},
                    "totalPrice": {"amount": 1200, "formattedPrice": "12,00 €"},
                    "affiliateUrl": "https://example.com/amazon_de",
                },
                {
                    "logo": {"name": "RAKUTEN"},
                    "condition": {"_name": "USED"},
                    "subCondition": {"name": "Très bon"},
                    "totalPrice": {"amount": 1500, "formattedPrice": "15,00 €"},
                    "affiliateUrl": "https://example.com/rakuten",
                },
            ]
        }
    }
    resp_lookup = httpx.Response(200, json=mock_lookup_with_amazon_de)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [resp_search, resp_rest, resp_prod, resp_lookup]

        offers = await provider.get_offers(isbn13="9791095670568", title="Rip Tome 1")
        assert len(offers) == 1
        offer = offers[0]
        assert offer.delivered_price_cents == 1500
        assert "Rakuten" in offer.item_condition
        assert "Amazon (De)" not in offer.item_condition
        assert "Amazon (De)" not in offer.raw_payload["partners"]


def test_select_historical_low_ignores_looser_condition_tiers():
    """Le plus bas 1 an ignore Bon état et l'occasion non qualifiée."""
    from app.providers.chasse_aux_livres import select_historical_low_cents

    assert select_historical_low_cents({"Bon état +": 4.5}) is None
    assert select_historical_low_cents({"D'occasion": 3.0, "Meilleur prix": 2.5}) is None
    assert (
        select_historical_low_cents(
            {"Bon état +": 3.0, "Très bon état +": 6.7, "Comme neuf +": 8.0}
        )
        == 670
    )


def test_select_historical_low_takes_the_minimum_across_accepted_tiers():
    """Le plus bas 1 an est le minimum des paliers acceptés, pas le premier trouvé.

    Régression Dead Train : « Comme neuf + » avait atteint 13,50 € quand
    « Très bon état + » s'arrêtait à 22,90 €. Prendre le premier libellé
    affichait une cote plus haute qu'un prix déjà constaté dans un état meilleur.
    """
    from app.providers.chasse_aux_livres import select_historical_low_cents

    assert select_historical_low_cents({"Très bon état +": 22.9, "Comme neuf +": 13.5}) == 1350
    # L'ordre des libellés ne doit plus changer le résultat.
    assert select_historical_low_cents({"Comme neuf +": 13.5, "Très bon état +": 22.9}) == 1350


@pytest.mark.asyncio
async def test_chasse_aux_livres_invalid_isbn_falls_back_to_title():
    """Un ISBN dont la clé de contrôle est fausse ne doit pas être utilisé pour la recherche."""
    provider = ChasseAuxLivresProvider()

    resp_search = httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
    resp_rest = httpx.Response(200, json=MOCK_REST_JSON)
    resp_prod = httpx.Response(200, text=MOCK_PROD_HTML)
    resp_lookup = httpx.Response(200, json=MOCK_LOOKUP_JSON)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [resp_search, resp_rest, resp_prod, resp_lookup]
        offers = await provider.get_offers(isbn13="9782365770000", title="Rip Tome 1")

    assert len(offers) == 1
    query_used = mock_get.call_args_list[0].args[0]
    assert "9782365770000" not in query_used


def test_is_title_coherent_rejects_unrelated_work_and_other_tome():
    """Un seul mot commun ne suffit plus à valider un titre."""
    is_coherent = ChasseAuxLivresProvider.is_title_coherent

    assert is_coherent("Rip Tome 1", "Rip Tome 1") is True
    assert is_coherent("Blacksad", "Blacksad Tome 5 Amarillo") is True
    assert (
        is_coherent("Gou Tanabe Lovecraft - Montagnes hallucinées", "Les Montagnes hallucinées")
        is True
    )

    assert is_coherent("Mutafukaz Loba Loca", "Rip Tome 1") is False
    assert is_coherent("Blacksad Tome 1", "Blacksad Tome 2") is False
    assert is_coherent("Le prophète", "Iznogoud") is False
    assert is_coherent("Le prophète", None) is True


@pytest.mark.parametrize("status", [403, 200])
async def test_historical_endpoint_failure_is_visible_even_with_a_current_offer(
    monkeypatch, status,
):
    client = AsyncMock()

    async def respond(url, **_kwargs):
        if "/rest/jcd" in url:
            return httpx.Response(status)
        if "/rest/lookup" in url:
            return httpx.Response(200, json=MOCK_LOOKUP_JSON)
        return httpx.Response(200, text=MOCK_PROD_HTML + '<div data-didh="aabbcc"></div>')

    client.get.side_effect = respond
    monkeypatch.setattr(
        "app.providers.chasse_aux_livres.resolve_session_token", AsyncMock(return_value="test")
    )
    offers = await ChasseAuxLivresProvider()._try_deep_lookup(
        client,
        MOCK_REST_JSON["d"],
        "Rip",
        expected_title="Rip",
        expected_isbn="9791095670568",
    )
    assert offers
    assert offers.failed_providers == ("chasse_aux_livres",)


def test_dated_history_preserves_point_date_price_and_condition():
    from datetime import UTC, datetime

    from app.providers.chasse_aux_livres import parse_historical_observations

    data = {"price_includes_shipping": True,
            "stats": [{"label": "Très bon état +", "statValues": [
        {"x": 1782864000000, "y": 5.45},
        {"y": 1.00},
    ]}, {"label": "Bon état +", "statValues": [{"x": 1782864000000, "y": 1}]}]}
    points = parse_historical_observations(data, "https://example.invalid/book")
    assert len(points) == 1
    assert points[0].observed_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert points[0].delivered_price_cents == 545
    assert points[0].is_historical
    assert not points[0].is_available


def test_history_without_delivery_proof_cannot_become_a_delivered_minimum():
    from app.providers.chasse_aux_livres import parse_historical_observations

    data = {"stats": [{"label": "Très bon état +", "statValues": [
        {"x": 1782864000000, "y": 5.45},
    ]}]}
    assert parse_historical_observations(data, "https://example.invalid/book") == []


def test_source_numeric_history_is_not_replaced_by_tooltip_text():
    from app.providers.chasse_aux_livres import parse_historical_observations

    data = {'price_includes_shipping': True, 'stats': [
        {'label': 'Très bon état +', 'statValues': [
            {'x': '1782864000000', 'y': '9.21', 'shipping': 0,
             't': '#[13,41 €'},
        ]},
    ]}
    points = parse_historical_observations(data, 'https://example.invalid/book')
    assert len(points) == 1
    assert points[0].delivered_price_cents == 921
    assert points[0].observed_at is not None


@pytest.mark.asyncio
async def test_source_item_and_shipping_fields_remain_separate():
    def response(request):
        if request.url.path == "/search":
            return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
        if request.url.path == "/rest/search-results":
            return httpx.Response(200, json=MOCK_REST_JSON)
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=MOCK_PROD_HTML)
        return httpx.Response(200, json={"offers": {"22": [{
            "logo": {"name": "MOMOX_SHOP"}, "condition": {"_name": "USED"},
            "subCondition": {"name": "Très bon"}, "price": {"amount": 1100},
            "shippingCost": {"amount": 200}, "totalPrice": {"amount": 1300},
            "affiliateUrl": "https://example.com/item",
        }]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        provider = ChasseAuxLivresProvider(http_client=client, max_retries=0)
        offers = await provider.get_offers(isbn13="9791095670568", title="Rip Tome 1")
    current = next(offer for offer in offers if offer.is_available)
    assert current.item_price_cents == 1100
    assert current.shipping_price_cents == 200
    assert current.delivered_price_cents == 1300


@pytest.mark.asyncio
async def test_collector_exception_logs_do_not_include_request_secrets(caplog):
    import logging

    async def unavailable(request):
        raise httpx.ReadTimeout("private-session-marker", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        provider = ChasseAuxLivresProvider(http_client=client, max_retries=0)
        with caplog.at_level(logging.DEBUG):
            result = await provider.get_offers(isbn13=None, title="Livre")
    assert result.failed_providers == ("chasse_aux_livres",)
    assert "private-session-marker" not in caplog.text
    assert "ReadTimeout" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "body"), [(429, ""), (200, "Vérification robot")])
async def test_source_restriction_stops_without_retries_or_title_fallback(status, body):
    calls = []

    async def limited(request):
        calls.append(request.url.path)
        return httpx.Response(status, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as client:
        provider = ChasseAuxLivresProvider(http_client=client)
        result = await provider.get_offers(isbn13="9782756079332", title="Jean Doux")
    assert calls == ["/search"]
    assert result.failed_providers == ("chasse_aux_livres",)
    assert result.attempts[0].reason in {"rate_limited", "bot_challenge"}


@pytest.mark.parametrize("response", [
    httpx.Response(503),
    httpx.Response(200, text="<html>Page temporairement incomplète</html>"),
])
async def test_isbn_source_failure_does_not_launch_a_title_search(response):
    requests = []

    def respond(request):
        requests.append(request)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ChasseAuxLivresProvider(http_client=client).get_offers(
            isbn13="9782808500487", title="Nanbanjin",
        )
    assert len(requests) == 1
    assert result.failed_providers == ("chasse_aux_livres",)
    assert result.attempts[0].stage in {"search_page", "search_metadata"}
    assert result.attempts[0].http_status == response.status_code


@pytest.mark.parametrize("stage", ["search_results", "product_page", "current_offers"])
async def test_source_restriction_at_each_stage_stops_without_title_fallback(stage):
    requests = []

    def respond(request):
        requests.append(request)
        path = request.url.path
        current_stage = ("search_results" if path == "/rest/search-results"
                         else "product_page" if path.startswith("/prix/")
                         else "current_offers" if path == "/rest/lookup/results"
                         else "search_page")
        if current_stage == stage:
            return httpx.Response(200, text="<title>Vérification robot</title>")
        if path == "/search":
            return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
        if path == "/rest/search-results":
            return httpx.Response(200, json=MOCK_REST_JSON)
        if path.startswith("/prix/"):
            return httpx.Response(200, text=MOCK_PROD_HTML)
        raise AssertionError(path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ChasseAuxLivresProvider(http_client=client).get_offers(
            isbn13="9791095670568", title="Rip Tome 1",
        )
    assert sum(r.url.path == "/search" for r in requests) == 1
    assert any(a.reason == "bot_challenge" and a.stage == stage for a in result.attempts)


async def test_history_restriction_keeps_current_offers_and_does_not_fallback(monkeypatch):
    from app.providers.base import requested_components

    requests = []
    monkeypatch.setattr('app.providers.chasse_aux_livres.resolve_session_token',
                        AsyncMock(return_value='synthetic-session'))
    monkeypatch.setattr('app.providers.chasse_aux_livres.chasse_session.get_history',
                        AsyncMock(return_value=httpx.Response(
                            200, text='<title>Vérification robot</title>')))
    def respond(request):
        requests.append(request)
        if request.url.path == '/search':
            return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
        if request.url.path == '/rest/search-results':
            return httpx.Response(200, json=MOCK_REST_JSON)
        if request.url.path.startswith('/prix/'):
            return httpx.Response(200, text=MOCK_PROD_HTML.replace('></div>',
                                  ' data-didh="abc123"></div>'))
        if request.url.path == '/rest/lookup/results':
            return httpx.Response(200, json={'offers': {'22': MOCK_LOOKUP_JSON['offers']['0']}})
        raise AssertionError(request.url.path)

    scope = requested_components.set(frozenset({'current', 'history'}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ChasseAuxLivresProvider(http_client=client).get_offers(
                isbn13='9791095670568', title='Rip Tome 1')
    finally:
        requested_components.reset(scope)
    assert len(result) == 1
    assert result[0].delivered_price_cents == 1499
    assert any(a.operation == 'history' and a.reason == 'bot_challenge'
               and a.stage == 'history' for a in result.attempts)
    assert not result.historical_checked
    assert sum(r.url.path == '/search' for r in requests) == 1


async def test_lookup_retry_restriction_preserves_already_received_offers(monkeypatch):
    requests = []
    history = AsyncMock()
    monkeypatch.setattr('app.providers.chasse_aux_livres.chasse_session.get_history', history)

    def respond(request):
        requests.append(request)
        if request.url.path == '/search':
            return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
        if request.url.path == '/rest/search-results':
            return httpx.Response(200, json=MOCK_REST_JSON)
        if request.url.path.startswith('/prix/'):
            return httpx.Response(200, text=MOCK_PROD_HTML.replace('></div>',
                                  ' data-didh="abc123"></div>'))
        if request.url.path == '/rest/lookup/results':
            if request.url.params.get('retry') == '1':
                return httpx.Response(200, text='<title>Vérification robot</title>')
            return httpx.Response(200, json=MOCK_LOOKUP_JSON)
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ChasseAuxLivresProvider(http_client=client).get_offers(
            isbn13='9791095670568', title='Rip Tome 1')
    assert len(result) == 1
    assert result[0].delivered_price_cents == 1499
    assert any(a.reason == 'bot_challenge' and a.stage == 'current_offers_retry'
               for a in result.attempts)
    history.assert_not_awaited()
    assert sum(r.url.path == '/search' for r in requests) == 1


@pytest.mark.parametrize("max_retries", [0, 2])
async def test_observed_empty_http_200_is_a_failed_collection_with_bounded_retry(max_retries):
    import json
    from pathlib import Path

    fixture = json.loads((Path(__file__).parents[1] / 'fixtures/chasse/'
                          'empty_search_results_2026_10_19.json').read_text())
    requests = []
    sleeper = AsyncMock()

    def response(request):
        requests.append(request.url.path)
        if request.url.path == '/search':
            return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
        return httpx.Response(fixture['status'], headers=fixture['headers'], text=fixture['body'])

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        provider = ChasseAuxLivresProvider(http_client=client, max_retries=max_retries,
                                          sleep=sleeper)
        offers = await provider.get_offers(isbn13='9791095670568', title='Rip Tome 1')
    assert offers == []
    attempt = offers.attempts[0]
    assert (attempt.reason, attempt.http_status, attempt.stage) == (
        'empty_response', 200, 'search_results',
    )
    assert attempt.response_content_type == 'text/plain;charset=utf-8'
    assert attempt.response_bytes == 0
    assert requests == ['/search', '/rest/search-results'] * (2 if max_retries else 1)
    assert sleeper.await_count == (1 if max_retries else 0)


async def test_empty_search_recovers_once_without_marking_a_source_restriction():
    calls = []
    sleeper = AsyncMock()

    def response(request):
        calls.append(request.url.path)
        if request.url.path == '/search':
            return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
        if calls.count('/rest/search-results') == 1:
            return httpx.Response(200, content=b'', headers={'content-type': 'text/plain'})
        return httpx.Response(200, json={'d': ''})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        provider = ChasseAuxLivresProvider(http_client=client, max_retries=2, sleep=sleeper)
        offers = await provider._search_query('9791095670568')
    assert offers == []
    assert not getattr(offers, 'failed_providers', ())
    assert calls == ['/search', '/rest/search-results'] * 2
    sleeper.assert_awaited_once_with(1.5)


@pytest.mark.parametrize(
    ('body', 'reason'), [('', 'empty_response'), ('not json', 'invalid_response')],
)
async def test_search_errors_keep_http_metadata_and_respect_retry_after(body, reason):
    requests = []
    sleeper = AsyncMock()

    def response(request):
        requests.append(request.url.path)
        if request.url.path == '/search':
            return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
        return httpx.Response(200, text=body, headers={'Retry-After': '7200'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        provider = ChasseAuxLivresProvider(http_client=client, max_retries=2, sleep=sleeper)
        offers = await provider._search_query('9791095670568')
    assert offers.attempts[0].reason == reason
    assert offers.attempts[0].http_status == 200
    assert offers.attempts[0].retry_after_seconds == 7200
    assert len(requests) == 2
    sleeper.assert_not_awaited()


@pytest.mark.parametrize(('url', 'isbn', 'accepted'), [
    ('https://www.chasse-aux-livres.fr/prix/2756079332/jean-doux-9782756079332',
     '9782756079332', True),
    ('https://www.chasse-aux-livres.fr/prix/2808500483/nanbanjin-9782808500487',
     '9782756079332', False),
    ('https://www.chasse-aux-livres.fr.evil.invalid/prix/9782756079332/book',
     '9782756079332', False),
    ('http://www.chasse-aux-livres.fr/prix/9782756079332/book', '9782756079332', False),
    ('https://user@www.chasse-aux-livres.fr/prix/9782756079332/book',
     '9782756079332', False),
    ('https://[', '9782756079332', False),
    ('https://www.chasse-aux-livres.fr/prix/9782756079332/book', None, False),
])
def test_cached_product_url_requires_the_source_host_and_same_edition(url, isbn, accepted):
    from app.providers.chasse_aux_livres import verified_product_url

    assert bool(verified_product_url(url, isbn)) == accepted


async def test_cached_product_skips_search_and_stops_on_restriction():
    requests = []
    sleeper = AsyncMock()

    def response(request):
        requests.append(request.url.path)
        assert request.url.path.startswith('/prix/')
        return httpx.Response(429, headers={'Retry-After': '3600'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        provider = ChasseAuxLivresProvider(http_client=client, max_retries=2, sleep=sleeper)
        offers = await provider.get_offers(
            isbn13='9782756079332', title='Jean Doux',
            source_url='https://www.chasse-aux-livres.fr/prix/2756079332/jean-doux-9782756079332',
        )
    assert len(requests) == 1
    assert offers.attempts[0].reason == 'rate_limited'
    assert offers.attempts[0].http_status == 429
    sleeper.assert_not_awaited()


@pytest.mark.parametrize(('groups', 'expected_status', 'expected_count'), [
    ({'0': [], '1': []}, 'partial', 0),
    ({'0': MOCK_LOOKUP_JSON['offers']['0'], '1': []}, 'partial', 1),
    ({'0': []}, 'partial', 0),
    ({'22': []}, 'partial', 0),
    ({'0': MOCK_LOOKUP_JSON['offers']['0']}, 'partial', 1),
    ({'0': [], '1': None}, 'partial', 0),
])
async def test_engine_groups_do_not_certify_offer_completion(
    groups, expected_status, expected_count,
):
    from app.providers.base import requested_components

    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.startswith('/prix/'):
            return httpx.Response(200, text=(
                '<div data-asin="2756079332" data-lvs="synthetic" data-nbeng="2"></div>'
            ))
        return httpx.Response(200, json={'offers': groups})

    scope = requested_components.set(frozenset({'current'}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ChasseAuxLivresProvider(http_client=client).get_offers(
                isbn13='9782756079332', title='Jean Doux',
                source_url='https://www.chasse-aux-livres.fr/prix/2756079332/9782756079332',
            )
    finally:
        requested_components.reset(scope)
    assert result.attempts[0].status == expected_status
    assert len(result) == expected_count
    assert len(requests) == (2 if '22' in groups else 3)
    if expected_status == 'partial':
        assert result.attempts[0].reason == 'incomplete_offers'
        for offer in result:
            assert offer.raw_payload['collection_status'] == 'partial'


async def test_observed_partial_offers_do_not_claim_absence_or_retry_search(monkeypatch):
    import json
    from pathlib import Path

    fixtures = Path(__file__).parents[1] / 'fixtures/chasse'
    partial = json.loads((fixtures / 'partial_offers_2026_10_19.json').read_text())['payload']
    history = json.loads((fixtures / 'jean_doux_history_2026_10_18.json').read_text())
    monkeypatch.setattr('app.providers.chasse_aux_livres.resolve_session_token',
                        AsyncMock(return_value='synthetic-session'))
    history_call = AsyncMock(return_value=httpx.Response(200, json=history))
    monkeypatch.setattr('app.providers.chasse_aux_livres.chasse_session.get_history', history_call)
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == '/search':
            return httpx.Response(200, text=MOCK_SEARCH_PAGE_HTML)
        if request.url.path == '/rest/search-results':
            return httpx.Response(200, json={'d': (
                '<a href="/prix/2756079332/9782756079332">Jean Doux</a>'
            )})
        if request.url.path.startswith('/prix/'):
            return httpx.Response(200, text=(
                '<div data-asin="2756079332" data-lvs="synthetic" '
                'data-nbeng="25" data-didh="abc123"></div>'
            ))
        return httpx.Response(200, json=partial)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ChasseAuxLivresProvider(http_client=client).get_offers(
            isbn13='9782756079332', title='Jean Doux',
        )
    assert requests == ['/search', '/rest/search-results', '/prix/2756079332/9782756079332',
                        '/rest/lookup/results', '/rest/lookup/results']
    assert result.attempts[0].status == 'partial'
    assert result.attempts[1].status == 'success'
    assert result.historical_checked
    assert all(not offer.is_available for offer in result)
    assert all(offer.item_condition != 'Occasion indisponible' for offer in result)
    history_call.assert_awaited_once()


async def test_malformed_current_offers_remain_technical_failure():
    from app.providers.base import requested_components

    def respond(request):
        if request.url.path.startswith('/prix/'):
            return httpx.Response(200, text=(
                '<div data-asin="2756079332" data-lvs="synthetic" data-nbeng="2"></div>'
            ))
        return httpx.Response(200, json={'offers': None})

    scope = requested_components.set(frozenset({'current'}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ChasseAuxLivresProvider(http_client=client).get_offers(
                isbn13='9782756079332', title='Jean Doux',
                source_url='https://www.chasse-aux-livres.fr/prix/2756079332/9782756079332',
            )
    finally:
        requested_components.reset(scope)
    assert result.attempts[0].status == 'failed'
    assert result.attempts[0].reason == 'invalid_response'
