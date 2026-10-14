"""Le provider de checkout Vinted : routes, corps, erreurs typées.

Toutes les réponses viennent d'un transport simulé : aucun appel réel à Vinted.
Les routes et les formes de corps testées ici sont celles lues dans le client
web Vinted, pas des formes supposées.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest

from app.domain.enums import VintedCheckoutStatus
from app.providers.vinted_checkout import (
    API_BASE_URL,
    CHECKOUT_SESSION_TTL_SECONDS,
    INQUIRIES_URL,
    REDIS_CHECKOUT_SESSION_KEY,
    SITE_URL,
    VintedCheckoutError,
    VintedCheckoutProvider,
    build_checkout_url_for,
    clear_checkout_cookies,
    resolve_checkout_cookies,
    session_is_authenticated,
    store_checkout_cookies,
)

COOKIES = {"access_token_web": "jeton-synthetique", "_vinted_fr_session": "session-synthetique"}

Handler = Callable[[httpx.Request], httpx.Response]


@asynccontextmanager
async def prepared_provider(handler: Handler) -> AsyncIterator[VintedCheckoutProvider]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield VintedCheckoutProvider(http_client=client, timeout_seconds=1.0)


def test_build_checkout_url_matches_the_web_client_shape() -> None:
    url = build_checkout_url_for(transaction_id="4242", purchase_id="98765")
    assert url == (
        f"{SITE_URL}/checkout?purchase_id=98765&order_id=4242&order_type=transaction"
    )


async def test_create_buyer_inquiry_posts_to_the_messaging_backend() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"conversation_id": 55, "transaction_id": 4242})

    async with prepared_provider(handler) as provider:
        transaction_id = await provider.create_buyer_inquiry(
            item_id="10373403907", seller_id="72804845", cookies=COOKIES
        )

    assert transaction_id == "4242"
    assert len(seen) == 1
    assert str(seen[0].url) == INQUIRIES_URL
    assert json.loads(seen[0].content) == {
        "item_ids": ["10373403907"],
        "receiver_id": 72804845,
    }
    assert seen[0].headers["authorization"] == "Bearer jeton-synthetique"
    assert "access_token_web" in seen[0].headers["cookie"]


async def test_create_buyer_inquiry_falls_back_on_route_not_found() -> None:
    """Un compte sur l'ancien backend de messagerie garde un chemin réel."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(404, json={"code": "NOT_FOUND"})
        return httpx.Response(
            200, json={"conversation": {"id": 7, "transaction": {"id": 4242}}}
        )

    async with prepared_provider(handler) as provider:
        transaction_id = await provider.create_buyer_inquiry(
            item_id="10373403907", seller_id="72804845", cookies=COOKIES
        )

    assert transaction_id == "4242"
    assert seen == [INQUIRIES_URL, f"{API_BASE_URL}/conversations"]


async def test_create_buyer_inquiry_does_not_fall_back_on_a_datadome_challenge() -> None:
    """Un défi anti-robot n'est pas un « backend absent » : aucune relance."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(403, headers={"x-dd-b": "1"}, text="blocked")

    async with prepared_provider(handler) as provider:
        with pytest.raises(VintedCheckoutError) as excinfo:
            await provider.create_buyer_inquiry(
                item_id="10373403907", seller_id="72804845", cookies=COOKIES
            )

    assert excinfo.value.status is VintedCheckoutStatus.BLOCKED
    assert len(seen) == 1


async def test_build_single_checkout_posts_transaction_items() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"checkout": {"id": 98765}})

    async with prepared_provider(handler) as provider:
        purchase_id = await provider.build_single_checkout(
            transaction_id="4242", cookies=COOKIES
        )

    assert purchase_id == "98765"
    assert str(seen[0].url) == f"{API_BASE_URL}/purchases/checkout/build"
    assert json.loads(seen[0].content) == {
        "purchase_items": [{"id": 4242, "type": "transaction"}]
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"message_code": "invalid_token"}),
        httpx.Response(403, json={"code": "FORBIDDEN", "message": "Access denied."}),
        httpx.Response(400, json={"message_code": "user_login_required"}),
    ],
)
async def test_authentication_refusals_map_to_auth_required(response: httpx.Response) -> None:
    async with prepared_provider(lambda _request: response) as provider:
        with pytest.raises(VintedCheckoutError) as excinfo:
            await provider.build_single_checkout(transaction_id="4242", cookies=COOKIES)
    assert excinfo.value.status is VintedCheckoutStatus.AUTH_REQUIRED


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(404, json={"code": "NOT_FOUND"}), VintedCheckoutStatus.UNAVAILABLE),
        (httpx.Response(410, text="gone"), VintedCheckoutStatus.UNAVAILABLE),
        (httpx.Response(422, json={"errors": []}), VintedCheckoutStatus.UNAVAILABLE),
        (httpx.Response(429, text="slow down"), VintedCheckoutStatus.RATE_LIMITED),
        (httpx.Response(500, text="boom"), VintedCheckoutStatus.UNEXPECTED),
        (httpx.Response(400, json={"message_code": "inconnu"}), VintedCheckoutStatus.UNEXPECTED),
    ],
)
async def test_status_mapping(
    response: httpx.Response, expected: VintedCheckoutStatus
) -> None:
    async with prepared_provider(lambda _request: response) as provider:
        with pytest.raises(VintedCheckoutError) as excinfo:
            await provider.build_single_checkout(transaction_id="4242", cookies=COOKIES)
    assert excinfo.value.status is expected


async def test_datadome_challenge_without_header_is_recognized_by_body() -> None:
    challenge = '<script src="https://geo.captcha-delivery.com/captcha/?initialCid=x"></script>'
    async with prepared_provider(lambda _request: httpx.Response(403, text=challenge)) as provider:
        with pytest.raises(VintedCheckoutError) as excinfo:
            await provider.build_single_checkout(transaction_id="4242", cookies=COOKIES)
    assert excinfo.value.status is VintedCheckoutStatus.BLOCKED


async def test_timeout_and_network_are_distinguished() -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout")

    def network(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic connect error")

    for handler, expected in (
        (timeout, VintedCheckoutStatus.TIMEOUT),
        (network, VintedCheckoutStatus.NETWORK),
    ):
        async with prepared_provider(handler) as provider:
            with pytest.raises(VintedCheckoutError) as excinfo:
                await provider.build_single_checkout(transaction_id="4242", cookies=COOKIES)
        assert excinfo.value.status is expected


async def test_missing_checkout_id_is_unexpected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"checkout": {}})
    async with prepared_provider(handler) as provider:
        with pytest.raises(VintedCheckoutError) as excinfo:
            await provider.build_single_checkout(transaction_id="4242", cookies=COOKIES)
    assert excinfo.value.status is VintedCheckoutStatus.UNEXPECTED


async def test_fetch_item_seller_id_reads_the_public_page() -> None:
    page = '... "seller_id":"72804845", "item_id":"10373403907" ...'
    async with prepared_provider(lambda _request: httpx.Response(200, text=page)) as provider:
        seller_id = await provider.fetch_item_seller_id("10373403907")
    assert seller_id == "72804845"


async def test_fetch_item_seller_id_returns_none_when_absent() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>sans identifiant</html>")
    async with prepared_provider(handler) as provider:
        seller_id = await provider.fetch_item_seller_id("10373403907")
    assert seller_id is None


async def test_fetch_item_seller_id_maps_a_gone_item_to_unavailable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page inexistante")
    async with prepared_provider(handler) as provider:
        with pytest.raises(VintedCheckoutError) as excinfo:
            await provider.fetch_item_seller_id("10373403907")
    assert excinfo.value.status is VintedCheckoutStatus.UNAVAILABLE


class _FakeRedis:
    """Le minimum Redis utilisé par la session de checkout, valeurs non exposées."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        del ex  # le double n'a pas d'expiration : seule la valeur compte
        self.store[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


async def test_checkout_session_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr("app.providers.vinted_checkout.get_redis_client", lambda: fake)

    assert await resolve_checkout_cookies() == {}
    assert not session_is_authenticated({})

    stored = await store_checkout_cookies(
        {
            "access_token_web": "jeton",
            "_vinted_fr_session": "session",
            "cookie_vide": "",
            "vide": "",
        }
    )
    assert stored == 2
    assert REDIS_CHECKOUT_SESSION_KEY in fake.store
    assert CHECKOUT_SESSION_TTL_SECONDS > 0

    cookies = await resolve_checkout_cookies()
    assert cookies == {"access_token_web": "jeton", "_vinted_fr_session": "session"}
    assert session_is_authenticated(cookies)

    await clear_checkout_cookies()
    assert await resolve_checkout_cookies() == {}


def test_checkout_session_without_identity_cookie_is_not_authenticated() -> None:
    assert not session_is_authenticated({"anon_id": "x"})


async def test_corrupted_checkout_session_reads_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    fake.store[REDIS_CHECKOUT_SESSION_KEY] = "{ceci n'est pas du json"
    monkeypatch.setattr("app.providers.vinted_checkout.get_redis_client", lambda: fake)
    assert await resolve_checkout_cookies() == {}


async def test_redis_failure_never_raises_on_read(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenRedis:
        async def get(self, _key: str) -> str | None:
            raise ConnectionError("synthetic redis down")

    monkeypatch.setattr("app.providers.vinted_checkout.get_redis_client", lambda: BrokenRedis())
    assert await resolve_checkout_cookies() == {}
