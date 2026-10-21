"""Rejeu des captures CAL du 21 octobre 2026 : signal de complétude et sondage.

Les réponses viennent de deux HAR réels. La règle démontrée est que la collecte
d'offres est complète quand l'ensemble des moteurs demandés a répondu : un moteur
qui a répondu sans rien trouver est présent avec une liste vide, un moteur muet
est absent. Le sondage renvoie à chaque tour les seuls moteurs encore attendus.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.providers.base import requested_components
from app.providers.chasse_aux_livres import (
    LOOKUP_POLL_MAX,
    ChasseAuxLivresProvider,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/chasse"
BASE = "https://www.chasse-aux-livres.fr"

JEAN_DOUX = {
    "isbn13": "9782756079332",
    "item_id": "2756079332",
    "path": "/prix/2756079332/9782756079332",
    "fixture": "lookup_jean_doux_2026_10_21.json",
}
NANBANJIN = {
    "isbn13": "9782808500487",
    "item_id": "2808500483",
    "path": "/prix/2808500483/9782808500487",
    "fixture": "lookup_nanbanjin_2026_10_21.json",
}


def _product_page(item_id: str, nbeng: int) -> str:
    return (
        f'<div data-asin="{item_id}" data-lvs="synthetic" '
        f'data-nbeng="{nbeng}" data-didh="abc123"></div>'
    )


def _capture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


async def _replay(book: dict, nbeng: int = 32) -> tuple:
    capture = _capture(book["fixture"])
    lookups: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=_product_page(book["item_id"], nbeng))
        lookups.append(request)
        retry = request.url.params.get("retry", "0")
        return httpx.Response(200, json=capture["responses"][retry])

    scope = requested_components.set(frozenset({"current"}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ChasseAuxLivresProvider(
                http_client=client, sleep=AsyncMock(),
            ).get_offers(
                isbn13=book["isbn13"],
                title="Ouvrage",
                source_url=BASE + book["path"],
            )
    finally:
        requested_components.reset(scope)
    return result, lookups


@pytest.mark.asyncio
async def test_jean_doux_capture_is_complete_without_polling():
    result, lookups = await _replay(JEAN_DOUX)

    attempt = result.attempts[0]
    assert attempt.status == "success"
    assert attempt.reason is None
    assert attempt.engines_requested == 32
    assert attempt.engines_received == 32
    assert attempt.poll_count == 0
    assert len(lookups) == 1

    offer = result[0]
    assert offer.delivered_price_cents == 1628
    assert offer.raw_payload["best_merchant"] == "Recyclivre"
    assert offer.raw_payload["partners"][offer.raw_payload["best_merchant"]][
        "total_cents"
    ] == 1628
    assert len(offer.raw_payload["partners"]) == 8
    assert offer.raw_payload["collection_status"] == "success"
    assert offer.raw_payload["engines_received"] == 32
    assert offer.raw_payload["poll_count"] == 0


@pytest.mark.asyncio
async def test_nanbanjin_capture_polls_only_the_missing_engines():
    result, lookups = await _replay(NANBANJIN)

    attempt = result.attempts[0]
    assert attempt.status == "success"
    assert attempt.engines_requested == 32
    assert attempt.engines_received == 32
    assert attempt.poll_count == LOOKUP_POLL_MAX
    assert attempt.reason is None

    assert [(r.url.params["retry"], r.url.params["engines"]) for r in lookups] == [
        ("0", "_".join(str(i) for i in range(32))),
        ("1", "0_1_22_30_31"),
        ("2", "1_30_31"),
        ("3", "30_31"),
    ]


@pytest.mark.asyncio
async def test_nanbanjin_complete_without_comparable_offer_is_a_confirmed_absence():
    result, _ = await _replay(NANBANJIN)

    assert list(result) == []
    assert result.attempts[0].status == "success"
    assert result.failed_providers == ()
    assert not result.historical_checked


@pytest.mark.asyncio
async def test_pending_engines_after_the_bound_never_assert_absence():
    partial = _capture("partial_offers_2026_10_19.json")["payload"]
    lookups: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=_product_page(JEAN_DOUX["item_id"], 25))
        lookups.append(request)
        return httpx.Response(200, json=partial)

    scope = requested_components.set(frozenset({"current"}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ChasseAuxLivresProvider(
                http_client=client, sleep=AsyncMock(),
            ).get_offers(
                isbn13=JEAN_DOUX["isbn13"],
                title="Jean Doux",
                source_url=BASE + JEAN_DOUX["path"],
            )
    finally:
        requested_components.reset(scope)

    attempt = result.attempts[0]
    assert attempt.status == "partial"
    assert attempt.reason == "incomplete_offers"
    assert attempt.engines_requested == 25
    assert attempt.engines_received == 4
    assert attempt.poll_count == LOOKUP_POLL_MAX
    assert len(lookups) == 1 + LOOKUP_POLL_MAX
    assert result.failed_providers == ("chasse_aux_livres",)


@pytest.mark.asyncio
@pytest.mark.parametrize("book", [JEAN_DOUX, NANBANJIN])
async def test_incomplete_pending_engines_never_expose_a_successful_collection(book):
    partial = _capture("partial_offers_2026_10_19.json")["payload"]

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=_product_page(book["item_id"], 32))
        return httpx.Response(200, json=partial)

    scope = requested_components.set(frozenset({"current"}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ChasseAuxLivresProvider(
                http_client=client, sleep=AsyncMock(),
            ).get_offers(
                isbn13=book["isbn13"],
                title="Ouvrage",
                source_url=BASE + book["path"],
            )
    finally:
        requested_components.reset(scope)

    attempt = result.attempts[0]
    assert attempt.engines_received < attempt.engines_requested
    assert attempt.status == "partial"
    for offer in result:
        assert offer.raw_payload["collection_status"] == "partial"
        assert offer.raw_payload["engines_received"] < offer.raw_payload["engines_requested"]


@pytest.mark.asyncio
async def test_product_page_without_engines_is_a_technical_failure():
    lookups: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=_product_page(JEAN_DOUX["item_id"], 0))
        lookups.append(request)
        return httpx.Response(200, json={"offers": {}})

    scope = requested_components.set(frozenset({"current"}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ChasseAuxLivresProvider(
                http_client=client, sleep=AsyncMock(),
            ).get_offers(
                isbn13=JEAN_DOUX["isbn13"],
                title="Jean Doux",
                source_url=BASE + JEAN_DOUX["path"],
            )
    finally:
        requested_components.reset(scope)

    assert result.attempts[0].status == "failed"
    assert result.attempts[0].reason == "invalid_response"
    assert lookups == []


@pytest.mark.asyncio
async def test_empty_poll_body_stops_as_partial_without_absence():
    offer = {
        "logo": {"name": "MOMOX_SHOP"},
        "condition": {"_name": "USED"},
        "subCondition": {"name": "Très bon"},
        "price": {"amount": 1200},
        "shippingCost": {"amount": 250},
        "totalPrice": {"amount": 1450},
        "affiliateUrl": "https://merchant.example/offer",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=_product_page(JEAN_DOUX["item_id"], 2))
        if request.url.params.get("retry") == "0":
            return httpx.Response(200, json={"offers": {"0": [offer]}})
        return httpx.Response(200, content=b"", headers={"content-type": "text/plain"})

    scope = requested_components.set(frozenset({"current"}))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await ChasseAuxLivresProvider(
                http_client=client, sleep=AsyncMock(),
            ).get_offers(
                isbn13=JEAN_DOUX["isbn13"],
                title="Jean Doux",
                source_url=BASE + JEAN_DOUX["path"],
            )
    finally:
        requested_components.reset(scope)

    attempt = result.attempts[0]
    assert attempt.status == "partial"
    assert attempt.poll_count == 1
    assert attempt.engines_received == 1
    assert result[0].delivered_price_cents == 1450
    assert result[0].raw_payload["collection_status"] == "partial"
    assert all(offer.item_condition != "Occasion indisponible" for offer in result)


@pytest.mark.asyncio
async def test_confirmed_absence_still_attempts_history_before_returning(monkeypatch):
    """Un current complet sans offre ne doit pas court-circuiter l'historique.

    L'historique est collecté dans `_try_deep_lookup`, donc avant le retour
    anticipé de `get_offers`. Ce test le verrouille : current complet sans offre
    comparable, historique demandé mais en échec, et aucune recherche par titre.
    """
    monkeypatch.setattr(
        "app.providers.chasse_aux_livres.resolve_session_token",
        AsyncMock(return_value="synthetic-session"),
    )
    history_call = AsyncMock(return_value=httpx.Response(500))
    monkeypatch.setattr(
        "app.providers.chasse_aux_livres.chasse_session.get_history", history_call,
    )
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/search":
            return httpx.Response(200, text=(
                '<div id="hash-cont" data-hash="mockhash1234567890abcdef12345678"></div>'
            ))
        if request.url.path == "/rest/search-results":
            return httpx.Response(200, json={
                "d": '<a href="/prix/2756079332/9782756079332">Jean Doux</a>',
            })
        if request.url.path.startswith("/prix/"):
            return httpx.Response(200, text=(
                '<div data-asin="2756079332" data-lvs="synthetic" '
                'data-nbeng="2" data-didh="abc123"></div>'
            ))
        return httpx.Response(200, json={"offers": {"0": [], "1": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ChasseAuxLivresProvider(
            http_client=client, sleep=AsyncMock(),
        ).get_offers(isbn13="9782756079332", title="Jean Doux")

    assert history_call.await_count == 1
    assert requests.count("/search") == 1
    assert [(attempt.operation, attempt.status) for attempt in result.attempts] == [
        ("current", "success"),
        ("history", "failed"),
    ]
    assert result.historical_checked is False
