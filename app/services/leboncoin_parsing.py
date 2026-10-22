"""Parsing pur des pages de recherche Leboncoin.

La page de recherche est un rendu Next.js : les annonces vivent dans le bloc
`__NEXT_DATA__`, sous `props.pageProps.searchData.ads`. Ce module ne fait aucun
réseau et ne connaît ni la base ni Telegram.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.enums import ItemCondition

_NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_PARIS = ZoneInfo("Europe/Paris")

SOURCE_NAME = "leboncoin"

# Codes d'état observés sur le flux Livres/BD, relevés sur les annonces réelles.
CONDITION_BY_CODE: dict[str, ItemCondition] = {
    "etatneuf": ItemCondition.NEW_WITHOUT_TAG,
    "tresbonetat": ItemCondition.VERY_GOOD,
    "bonetat": ItemCondition.GOOD,
    "etatsatisfaisant": ItemCondition.SATISFACTORY,
}


class LeboncoinParseError(ValueError):
    """La page reçue n'a pas la structure d'une recherche Leboncoin."""


@dataclass(frozen=True)
class ScrapedLeboncoinItem:
    """Annonce extraite d'une page de recherche, avant normalisation métier."""

    external_id: str
    title: str
    url: str
    item_price_cents: int
    condition: ItemCondition | None
    photo_url: str | None
    published_at: datetime | None


def _price_cents(price: Any) -> int | None:
    """Prix Leboncoin en euros (liste d'un entier) converti en centimes."""
    if isinstance(price, list):
        if not price:
            return None
        value = price[0]
    else:
        value = price
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(round(float(value) * 100))


def _first_https_url(images: Any) -> str | None:
    """Première URL HTTPS d'image, que `images` soit un objet ou une liste."""
    candidates: list[Any] = []
    if isinstance(images, dict):
        candidates = list(images.get("urls") or [])
    elif isinstance(images, list):
        for entry in images:
            if isinstance(entry, str):
                candidates.append(entry)
            elif isinstance(entry, dict):
                candidates.extend(entry.get("urls") or [])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith("https://"):
            return candidate
    return None


def _published_at(value: Any) -> datetime | None:
    """Horodatage Leboncoin naïf (heure de Paris) converti en UTC."""
    if not isinstance(value, str):
        return None
    try:
        naive = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=_PARIS).astimezone(UTC)


def _condition(attributes: Any) -> ItemCondition | None:
    """État déclaré, mappé sur le référentiel commun ; un code inconnu reste absent."""
    if not isinstance(attributes, list):
        return None
    for entry in attributes:
        if not isinstance(entry, dict) or entry.get("key") != "condition":
            continue
        code = entry.get("value")
        if isinstance(code, str):
            return CONDITION_BY_CODE.get(code.lower())
        return None
    return None


def parse_search_page(html: str) -> list[ScrapedLeboncoinItem]:
    """Extrait les annonces d'une page de recherche, dans l'ordre reçu."""
    match = _NEXT_DATA_PATTERN.search(html)
    if match is None:
        raise LeboncoinParseError("__NEXT_DATA__ absent de la page")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise LeboncoinParseError("__NEXT_DATA__ illisible") from exc

    page_props = (data.get("props") or {}).get("pageProps") or {}
    search_data = page_props.get("searchData")
    if not isinstance(search_data, dict):
        raise LeboncoinParseError("searchData absent")
    ads = search_data.get("ads")
    if not isinstance(ads, list):
        raise LeboncoinParseError("ads absent")

    items: list[ScrapedLeboncoinItem] = []
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        external_id = str(ad.get("list_id") or "").strip()
        title = str(ad.get("subject") or "").strip()
        url = str(ad.get("url") or "").strip()
        price_cents = _price_cents(ad.get("price"))
        if not external_id or not title or not url.startswith("https://") or price_cents is None:
            continue
        items.append(
            ScrapedLeboncoinItem(
                external_id=external_id,
                title=title,
                url=url,
                item_price_cents=price_cents,
                condition=_condition(ad.get("attributes")),
                photo_url=_first_https_url(ad.get("images")),
                published_at=_published_at(ad.get("first_publication_date")),
            )
        )
    return items


def to_ingest_payload(
    item: ScrapedLeboncoinItem,
    category_slug: str = "bd_comics",
) -> dict[str, Any]:
    """Convertit une annonce en payload d'ingestion, sans inventer vendeur ni frais."""
    payload: dict[str, Any] = {
        "source_name": SOURCE_NAME,
        "external_id": item.external_id,
        "url": item.url,
        "title": item.title,
        "category_slug": category_slug,
        "item_price_cents": item.item_price_cents,
        "condition": item.condition.value if item.condition is not None else None,
        "photos": [item.photo_url] if item.photo_url else [],
    }
    if item.published_at is not None:
        payload["published_at"] = item.published_at.isoformat()
    return payload
