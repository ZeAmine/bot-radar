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
        # Deux passes × quatre réponses : elles doivent aboutir sans être avalées par le retry.
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

        offers = await provider.get_offers(isbn13="9791095670568", title="Rip Tome 1")

    assert len(offers) == 0
    assert _search_call_count(mock_get) == 2
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
