"""Tests du parsing pur des pages de recherche Leboncoin."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domain.enums import ItemCondition
from app.services.leboncoin_parsing import (
    LeboncoinParseError,
    parse_search_page,
    to_ingest_payload,
)


def _page(ads: list[dict]) -> str:
    data = {"props": {"pageProps": {"searchData": {"ads": ads}}}}
    return (
        "<html><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data)
        + "</script></body></html>"
    )


def _ad(**overrides: object) -> dict:
    ad: dict = {
        "list_id": 3291629617,
        "subject": "Blacksad — Intégrale",
        "url": "https://www.leboncoin.fr/ad/livres/3291629617",
        "price": [25],
        "first_publication_date": "2026-10-19 23:48:20",
        "images": {"urls": ["https://img.leboncoin.fr/x.jpg"]},
        "attributes": [
            {"key": "condition", "value": "tresbonetat", "value_label": "Très bon état"}
        ],
    }
    ad.update(overrides)
    return ad


def test_parse_search_page_extracts_fields():
    items = parse_search_page(_page([_ad()]))

    assert len(items) == 1
    item = items[0]
    assert item.external_id == "3291629617"
    assert item.title == "Blacksad — Intégrale"
    assert item.url == "https://www.leboncoin.fr/ad/livres/3291629617"
    assert item.item_price_cents == 2500
    assert item.condition is ItemCondition.VERY_GOOD
    assert item.photo_url == "https://img.leboncoin.fr/x.jpg"
    assert item.published_at == datetime(2026, 10, 19, 21, 48, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("etatneuf", ItemCondition.NEW_WITHOUT_TAG),
        ("tresbonetat", ItemCondition.VERY_GOOD),
        ("bonetat", ItemCondition.GOOD),
        ("etatsatisfaisant", ItemCondition.SATISFACTORY),
    ],
)
def test_parse_condition_codes(code: str, expected: ItemCondition):
    ad = _ad(attributes=[{"key": "condition", "value": code}])
    assert parse_search_page(_page([ad]))[0].condition is expected


def test_unknown_condition_stays_absent():
    ad = _ad(attributes=[{"key": "condition", "value": "etat_mystere"}])
    assert parse_search_page(_page([ad]))[0].condition is None
    assert parse_search_page(_page([_ad(attributes=[])]))[0].condition is None


def test_malformed_ads_are_ignored():
    ads = [
        _ad(list_id=1),
        _ad(list_id=None),
        _ad(list_id=2, url="http://www.leboncoin.fr/ad/livres/2"),
        _ad(list_id=3, price=[]),
        _ad(list_id=4, subject=""),
        "pas un dictionnaire",
    ]
    items = parse_search_page(_page(ads))
    assert [item.external_id for item in items] == ["1"]


def test_missing_next_data_raises():
    with pytest.raises(LeboncoinParseError):
        parse_search_page("<html><body>Please enable JS</body></html>")


def test_missing_search_data_raises():
    html = '<script id="__NEXT_DATA__">{"props":{"pageProps":{}}}</script>'
    with pytest.raises(LeboncoinParseError):
        parse_search_page(html)


def test_empty_ads_returns_empty_list():
    assert parse_search_page(_page([])) == []


def test_no_photo_yields_empty_photos():
    item = parse_search_page(_page([_ad(images={"urls": []})]))[0]
    assert item.photo_url is None
    assert to_ingest_payload(item)["photos"] == []


def test_images_as_list_of_objects():
    ad = _ad(images=[{"urls": ["https://img.leboncoin.fr/a.jpg"]}])
    assert parse_search_page(_page([ad]))[0].photo_url == "https://img.leboncoin.fr/a.jpg"


def test_to_ingest_payload_keeps_source_without_fees_or_seller():
    item = parse_search_page(_page([_ad()]))[0]
    payload = to_ingest_payload(item)

    assert payload["source_name"] == "leboncoin"
    assert payload["category_slug"] == "bd_comics"
    assert payload["external_id"] == "3291629617"
    assert payload["condition"] == "Très bon état"
    assert payload["photos"] == ["https://img.leboncoin.fr/x.jpg"]
    assert payload["published_at"] == "2026-10-19T21:48:20+00:00"
    assert "estimated_protection_cents" not in payload
    assert "estimated_shipping_cents" not in payload
    assert "seller_name" not in payload


FIXTURE_PAGE = Path(__file__).resolve().parents[1] / "fixtures" / "leboncoin" / "search_page.json"


def test_captured_search_page_fixture_parses_with_expected_fields():
    payload = json.loads(FIXTURE_PAGE.read_text(encoding="utf-8"))
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )

    items = parse_search_page(html)

    assert [item.external_id for item in items] == ["900000001", "900000002", "900000003"]
    assert [item.item_price_cents for item in items] == [500, 1200, 700]
    assert items[0].published_at == datetime(2026, 10, 19, 23, 37, 26, tzinfo=UTC)
    assert items[0].condition is ItemCondition.NEW_WITHOUT_TAG
    assert items[2].condition is ItemCondition.GOOD
    assert items[0].photo_url == "https://img.example/leboncoin-1.jpg"
