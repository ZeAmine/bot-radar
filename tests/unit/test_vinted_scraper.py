import base64
import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.vinted_scraper import (
    VintedSessionManager,
    extract_all_catalog_photos,
    extract_item_page_photos,
    extract_photo_url,
    get_jwt_exp,
    is_session_refresh_page,
    parse_vinted_catalog_html,
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


