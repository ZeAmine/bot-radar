import base64
import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.vinted_scraper import (
    VintedSessionManager,
    catalog_url_to_api_params,
    extract_all_catalog_photos,
    extract_item_page_photos,
    extract_photo_url,
    fetch_vinted_page,
    get_jwt_exp,
    is_session_refresh_page,
    parse_vinted_catalog_html,
    parse_vinted_catalog_json,
    parse_vinted_overlay_title,
    to_ingest_payload,
)

SAMPLE_HTML = """
<div class="feed-grid">
  <div class="ItemBox-module-scss-module__NoC3Da__new-item-box__container" data-testid="product-item-id-9951815312">  <!-- noqa: E501 -->
    <div class="u-position-relative">
      <img src="https://images1.vinted.net/t/06_014ae_RnaLhBk7abUFqqtKyJagcm56/310x430/863258e3.webp?s=f364a40935bf7f832a7d44b11e3ac03363305b7a" alt="Bd RIP tome 1" />  <!-- noqa: E501 -->
    </div>
    <a data-testid="product-item-id-9951815312--overlay-link" title="Bd RIP tome 1, État: Neuf sans étiquette, 15.00 €, 16.45 €" href="/items/9951815312-bd-rip-tome-1"></a>  <!-- noqa: E501 -->
  </div>

  <div class="ItemBox-module-scss-module__NoC3Da__new-item-box__container" data-testid="product-item-id-9950549316">  <!-- noqa: E501 -->
    <div class="u-position-relative">
      <img src="https://images1.vinted.net/t/sample2.webp" alt="BD RIP intégrale" />
    </div>
    <a data-testid="product-item-id-9950549316--overlay-link" title="BD &quot;RIP&quot;, intégrale, État: Très bon état, 80.00 €, 84.70 €" href="/items/9950549316-bd-rip-integrale"></a>  <!-- noqa: E501 -->
  </div>
</div>
"""  # noqa: E501


def test_parse_vinted_overlay_title():
    raw1 = "Bd RIP tome 1, État: Neuf sans étiquette, 15.00 €, 16.45 €"
    title, cond, price = parse_vinted_overlay_title(raw1)
    assert title == "Bd RIP tome 1"
    assert cond == "Neuf sans étiquette"
    assert price == 1500

    raw2 = 'BD "RIP", intégrale, État: Très bon état, 80.50 €, 85.20 €'
    title2, cond2, price2 = parse_vinted_overlay_title(raw2)
    assert title2 == 'BD "RIP", intégrale'
    assert cond2 == "Très bon état"
    assert price2 == 8050


def test_extract_photo_url():
    url = extract_photo_url(SAMPLE_HTML, "9951815312")
    assert url is not None
    assert "863258e3.webp" in url


def test_parse_vinted_catalog_html():
    items = parse_vinted_catalog_html(SAMPLE_HTML)
    assert len(items) == 2

    first = items[0]
    assert first.external_id == "9951815312"
    assert first.title == "Bd RIP tome 1"
    assert first.condition == "Neuf sans étiquette"
    assert first.item_price_cents == 1500
    assert first.url == "https://www.vinted.fr/items/9951815312"
    assert first.photo_url is not None

    second = items[1]
    assert second.external_id == "9950549316"
    assert second.title == 'BD "RIP", intégrale'
    assert second.item_price_cents == 8000


def test_to_ingest_payload():
    items = parse_vinted_catalog_html(SAMPLE_HTML)
    payload = to_ingest_payload(items[0])

    assert payload["source_name"] == "vinted"
    assert payload["external_id"] == "9951815312"
    assert payload["item_price_cents"] == 1500
    assert payload["estimated_shipping_cents"] == 288
    assert payload["estimated_protection_cents"] == 70 + int(1500 * 0.05)
    assert payload["condition"] == "Neuf sans étiquette"
    assert len(payload["photos"]) == 1


def test_extract_all_catalog_photos_modern_html():
    sample = (
        '<img src="https://images1.vinted.net/t/01/310x430/aaa.webp" '
        'data-testid="product-item-id-111--image--img" />\n'
        '<img data-testid="product-item-id-222--image--img" '
        'src="https://images1.vinted.net/t/02/310x430/bbb.webp" />'
    )
    photos = extract_all_catalog_photos(sample)
    assert photos.get("111") == "https://images1.vinted.net/t/01/310x430/aaa.webp"
    assert photos.get("222") == "https://images1.vinted.net/t/02/310x430/bbb.webp"


def test_extract_item_page_photos():
    sample = """
    <script>
      "photos": ["https:\\/\\/images1.vinted.net\\/t\\/01\\/f800\\/abc12345.webp?s=123",
                 "https:\\/\\/images1.vinted.net\\/t\\/01\\/f800\\/abc12345.webp?s=456",
                 "https:\\/\\/images1.vinted.net\\/t\\/02\\/f800\\/def67890.webp?s=789"]
    </script>
    """
    photos = extract_item_page_photos(sample)
    assert len(photos) == 2
    assert "abc12345.webp" in photos[0]
    assert "def67890.webp" in photos[1]


def test_extract_item_page_photos_excludes_seller_avatar():
    sample = """
    "photos":[{"height":1600,"is_main":true,"thumbnails":[{"url":"https://images1.vinted.net/t/01/f800/item1111.webp?s=1"}]},
              {"height":1600,"is_main":false,"thumbnails":[{"url":"https://images1.vinted.net/t/02/f800/item2222.webp?s=2"}]}],
    "user":{"id":123,"photo":{"height":0,"thumbnails":[{"url":"https://images1.vinted.net/t/03/f800/avatar99.webp?s=3"}]}}
    """
    photos = extract_item_page_photos(sample)
    assert len(photos) == 2
    assert "item1111.webp" in photos[0]
    assert "item2222.webp" in photos[1]
    assert not any("avatar99.webp" in p for p in photos)


def test_avatar_filtering_excludes_seller_profile_pictures():
    sample_with_avatar = """
    <div class="ItemBox" data-testid="product-item-id-999999">
      <div class="seller-avatar">
        <img src="https://images1.vinted.net/t/01/users/avatar_123.jpg" alt="Seller" />
      </div>
      <div class="product-photo">
        <img src="https://images1.vinted.net/t/01/310x430/item_456.jpg"
             data-testid="product-item-id-999999--image--img" />
      </div>
    </div>
    """
    photo_url = extract_photo_url(sample_with_avatar, "999999")
    assert photo_url == "https://images1.vinted.net/t/01/310x430/item_456.jpg"
    assert "avatar" not in photo_url


@pytest.mark.asyncio
async def test_vinted_session_manager_manual_and_redis():
    mgr = VintedSessionManager()
    assert mgr.cookies == {}

    # Test injection manuelle avec mock Redis
    with patch.object(mgr, "_save_cookies_to_redis", new_callable=AsyncMock) as mock_save:
        await mgr.set_manual_session(
            {"anon_id": "test-anon-123", "access_token_web": "jwt-token-456"}
        )
        assert mgr.cookies.get("anon_id") == "test-anon-123"
        mock_save.assert_awaited_once()

    headers = mgr.get_auth_headers()
    assert headers["Authorization"] == "Bearer jwt-token-456"
    assert "Chrome/131" in headers["User-Agent"]


@pytest.mark.asyncio
async def test_vinted_session_manager_load_from_redis():
    mgr = VintedSessionManager()
    fake_client = AsyncMock()
    fake_client.cookies = httpx.Cookies()

    with patch.object(mgr, "_load_cookies_from_redis", new_callable=AsyncMock) as mock_load:
        mock_load.return_value = {
            "anon_id": "cached-redis-id",
            "access_token_web": "cached-token",
        }
        await mgr.ensure_valid_session(fake_client)

        assert mgr.cookies.get("anon_id") == "cached-redis-id"
        assert fake_client.cookies.get("anon_id") == "cached-redis-id"
        assert fake_client.cookies.get("access_token_web") == "cached-token"


def test_is_session_refresh_page_and_jwt_exp():
    assert is_session_refresh_page("<html><body>Session refresh</body></html>") is True
    assert is_session_refresh_page("") is True
    assert is_session_refresh_page("small page") is True
    assert (
        is_session_refresh_page(
            '<html><div data-testid="product-item-id-123"></div>' + "x" * 90000
        )
        is False
    )

    # Test JWT decode
    now = int(time.time())
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode("utf-8").rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": now + 3600}).encode("utf-8"))
        .decode("utf-8")
        .rstrip("=")
    )
    token = f"{header}.{payload}.signature"
    exp = get_jwt_exp(token)
    assert exp == float(now + 3600)


def test_catalog_url_to_api_params():
    url = (
        "https://www.vinted.fr/catalog?catalog[]=5425"
        "&status_ids[]=1&status_ids[]=2&language_book_ids[]=6436"
        "&search_id=12345&order=newest_first"
    )
    params = catalog_url_to_api_params(url)
    assert params["attribute_ids[catalog]"] == "5425"
    assert params["attribute_ids[status]"] == "1,2"
    assert params["attribute_ids[language_book]"] == "6436"
    assert params["order"] == "newest_first"
    assert params["per_page"] == "24"

    url_search = "https://www.vinted.fr/catalog?search_text=zelda+switch"
    params_search = catalog_url_to_api_params(url_search)
    assert params_search["search_text"] == "zelda switch"
    assert params_search["order"] == "newest_first"


def test_parse_vinted_catalog_json():
    sample_json = {
        "items": [
            {
                "id": 10113230090,
                "title": "Les legendes marvel n°3 : 1966",
                "price": {"amount": "4.00", "currency_code": "EUR"},
                "item_box": {
                    "second_line": "Neuf sans étiquette",
                    "accessibility_label": "Marvel, État: Neuf sans étiquette, 4.00 €",
                },
                "url": "/items/10113230090-les-legendes-marvel",
                "photo": {
                    "url": "https://images1.vinted.net/t/01/f800/main.webp?s=123"
                },
                "photos": [
                    {"url": "https://images1.vinted.net/t/01/f800/main.webp?s=123"},
                    {"url": "https://images1.vinted.net/t/02/f800/extra1.webp?s=456"},
                    {"url": "https://images1.vinted.net/t/03/f800/extra2.webp?s=789"},
                ],
            },
            {
                "id": 10113228948,
                "title": "Manga",
                "price": {"amount": "0.00"},  # Price 0 should be skipped
            },
        ]
    }
    items = parse_vinted_catalog_json(sample_json)
    assert len(items) == 1

    item = items[0]
    assert item.external_id == "10113230090"
    assert item.title == "Les legendes marvel n°3 : 1966"
    assert item.item_price_cents == 400
    assert item.condition == "Neuf sans étiquette"
    assert item.url == "https://www.vinted.fr/items/10113230090-les-legendes-marvel"
    assert item.photo_url == "https://images1.vinted.net/t/01/f800/main.webp?s=123"
    assert len(item.extra_photos) == 2
    assert "extra1.webp" in item.extra_photos[0]
    assert "extra2.webp" in item.extra_photos[1]

    # Test to_ingest_payload with extra photos
    payload = to_ingest_payload(item)
    assert payload["external_id"] == "10113230090"
    assert payload["item_price_cents"] == 400
    assert len(payload["photos"]) == 3
    assert payload["photos"][0] == "https://images1.vinted.net/t/01/f800/main.webp?s=123"


def test_vinted_session_manager_api_headers():
    mgr = VintedSessionManager()
    mgr.cookies = {"access_token_web": "fake-jwt", "anon_id": "anon-456"}
    headers = mgr.get_api_headers()
    assert headers["Accept"] == "application/json, text/plain, */*"
    assert headers["Authorization"] == "Bearer fake-jwt"
    assert headers["X-Anon-Id"] == "anon-456"
    assert headers["Platform"] == "web"
    assert headers["X-Next-App"] == "marketplace-web"


@pytest.mark.asyncio
async def test_fetch_vinted_page_api_success():
    fake_client = AsyncMock()
    fake_client.cookies = httpx.Cookies()
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json = lambda: {
        "items": [
            {
                "id": 12345,
                "title": "BD Tintin",
                "price": {"amount": "12.50"},
                "item_box": {"second_line": "Très bon état"},
                "url": "/items/12345-bd-tintin",
            }
        ]
    }
    mock_resp.text = '{"items": [{"id": 12345}]}'
    fake_client.get.return_value = mock_resp

    target = "app.services.vinted_scraper.vinted_session_manager.ensure_valid_session"
    with patch(target, new_callable=AsyncMock):
        items = await fetch_vinted_page(
            "https://www.vinted.fr/catalog?search_text=tintin",
            fake_client,
        )
    assert len(items) == 1
    assert items[0].external_id == "12345"
    assert items[0].title == "BD Tintin"
    assert items[0].item_price_cents == 1250


@pytest.mark.asyncio
async def test_fetch_vinted_page_fallback_to_html():
    fake_client = AsyncMock()
    fake_client.cookies = httpx.Cookies()
    # 1er appel (API): statut 500
    api_resp = AsyncMock()
    api_resp.status_code = 500
    api_resp.headers = {"content-type": "text/html"}

    # 2e appel (HTML fallback): statut 200 avec SAMPLE_HTML
    html_resp = AsyncMock()
    html_resp.status_code = 200
    html_resp.headers = {"content-type": "text/html"}
    html_resp.text = SAMPLE_HTML

    fake_client.get.side_effect = [api_resp, html_resp]

    target = "app.services.vinted_scraper.vinted_session_manager.ensure_valid_session"
    with patch(target, new_callable=AsyncMock):
        items = await fetch_vinted_page("https://www.vinted.fr/catalog", fake_client)
    assert len(items) == 2
    assert items[0].external_id == "9951815312"
    assert items[0].title == "Bd RIP tome 1"


@pytest.mark.asyncio
async def test_poll_vinted_once_ingests_in_process():
    """Le poller ingère dans le processus : Vinted seul, pas de POST localhost."""
    from app.services.vinted_scraper import ScrapedVintedItem
    from app.tasks.vinted_poller import poll_vinted_once

    mock_vinted_client = AsyncMock()
    dummy_item = ScrapedVintedItem(
        external_id="v_999",
        url="https://vinted.fr/items/999",
        title="Tintin au Tibet",
        condition="Très bon état",
        item_price_cents=1000,
        photo_url="https://images1.vinted.net/t.jpg",
    )
    seen_ids: set[str] = set()
    expected_url = (
        "https://www.vinted.fr/catalog?search_text=&catalog[]=5425&status_ids[]=1"
        "&status_ids[]=2&status_ids[]=6&language_book_ids[]=6436"
        "&search_id=2946920741&order=newest_first"
    )

    with (
        patch("app.tasks.vinted_poller.fetch_vinted_page", new_callable=AsyncMock) as mock_fetch,
        patch(
            "app.tasks.vinted_poller._ingest_scraped_item",
            new_callable=AsyncMock,
            return_value=0,
        ) as mock_ingest,
    ):
        mock_fetch.return_value = [dummy_item]
        count = await poll_vinted_once(seen_ids=seen_ids, vinted_client=mock_vinted_client)

    assert count == 1
    assert "v_999" in seen_ids
    mock_fetch.assert_called_once_with(expected_url, mock_vinted_client)
    mock_ingest.assert_awaited_once()
    mock_vinted_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_vinted_session_manager_proactive_jwt_refresh():
    """Vérifie que la rotation proactive se déclenche quand le jeton expire dans < 30 minutes."""
    mgr = VintedSessionManager()
    now = int(time.time())

    # Jeton expirant dans 10 minutes (600s < 1800s)
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode("utf-8").rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": now + 600}).encode("utf-8"))
        .decode("utf-8")
        .rstrip("=")
    )
    expiring_token = f"{header}.{payload}.signature"
    mgr.cookies = {"access_token_web": expiring_token, "anon_id": "test-anon"}
    mgr.last_refreshed = now

    fake_client = AsyncMock()
    fake_client.cookies = httpx.Cookies()

    with patch.object(mgr, "refresh_session", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = True
        await mgr.ensure_valid_session(fake_client)
        mock_refresh.assert_awaited_once_with(fake_client)


@pytest.mark.asyncio
async def test_vinted_session_manager_atomic_clear_session():
    """Vérifie que clear_session purge la RAM, le jar client et la clé Redis."""
    mgr = VintedSessionManager()
    mgr.cookies = {"access_token_web": "some-token", "anon_id": "anon-123"}
    mgr.last_refreshed = time.time()

    fake_client = AsyncMock()
    fake_cookies = httpx.Cookies()
    fake_cookies.set("access_token_web", "some-token", domain=".vinted.fr")
    fake_client.cookies = fake_cookies

    mock_redis = AsyncMock()
    with patch("app.services.vinted_scraper.get_redis_client", return_value=mock_redis):
        await mgr.clear_session(fake_client)

    assert mgr.cookies == {}
    assert mgr.last_refreshed == 0
    assert len(fake_client.cookies) == 0
    mock_redis.delete.assert_awaited_once_with("radar:vinted_session_cookies")


@pytest.mark.asyncio
async def test_fetch_vinted_page_401_self_healing_retry():
    """Vérifie l'auto-guérison immédiate et le rejeu en cas de réponse API 401."""
    fake_client = AsyncMock()
    fake_client.cookies = httpx.Cookies()

    # 1er appel API: 401
    resp_401 = AsyncMock()
    resp_401.status_code = 401

    # 2e appel API (rejeu après refresh): 200 OK
    resp_200 = AsyncMock()
    resp_200.status_code = 200
    resp_200.headers = {"content-type": "application/json"}
    resp_200.json = lambda: {
        "items": [
            {
                "id": 98765,
                "title": "Astérix le Gaulois",
                "price": {"amount": "8.00"},
                "url": "/items/98765-asterix",
            }
        ]
    }
    resp_200.text = '{"items": [{"id": 98765}]}'

    fake_client.get.side_effect = [resp_401, resp_200]

    with (
        patch(
            "app.services.vinted_scraper.vinted_session_manager.ensure_valid_session",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.vinted_scraper.vinted_session_manager.clear_session",
            new_callable=AsyncMock,
        ) as mock_clear,
        patch(
            "app.services.vinted_scraper.vinted_session_manager.refresh_session",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_refresh,
    ):
        items = await fetch_vinted_page("https://www.vinted.fr/catalog?catalog[]=5425", fake_client)

    assert len(items) == 1
    assert items[0].external_id == "98765"
    assert items[0].title == "Astérix le Gaulois"
    mock_clear.assert_awaited_once_with(fake_client)
    mock_refresh.assert_awaited_once_with(fake_client)
    assert fake_client.get.call_count == 2


