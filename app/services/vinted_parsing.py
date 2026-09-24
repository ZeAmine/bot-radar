"""Parsing pur du catalogue Vinted (HTML et JSON). Aucun réseau."""

import html
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

OVERLAY_LINK_REGEX = re.compile(
    r'data-testid="product-item-id-(\d+)--overlay-link"[^>]*title="([^"]+)"',
    re.DOTALL,
)

@dataclass
class ScrapedVintedItem:
    """Représente une annonce extraite depuis le flux Vinted."""

    external_id: str
    title: str
    condition: str
    item_price_cents: int
    url: str
    photo_url: str | None
    extra_photos: list[str] = field(default_factory=list)


def parse_vinted_overlay_title(raw_title: str) -> tuple[str, str, int]:
    """Parse le titre de l'overlay Vinted.

    Exemple Vinted :
    'Bd Rip  Tome 1 je ne survivrai pas à la mort, État: Très bon état, 5.00 €, 5.95 €'
    Retourne : (titre, condition, prix_en_centimes)
    """
    decoded = html.unescape(raw_title).strip()

    title = decoded
    condition = "Très bon état"
    price_cents = 0

    if ", État: " in decoded:
        title_part, rest = decoded.split(", État: ", 1)
        title = title_part.strip()
        parts = rest.rsplit(", ", 2)
        if len(parts) >= 2:
            condition = parts[0].strip()
            price_str = parts[1].replace("€", "").strip()
            try:
                price_cents = round(float(price_str.replace(",", ".")) * 100)
            except ValueError:
                price_cents = 0
    else:
        # Fallback si format différent
        match_price = re.search(r"([0-9.,]+)\s*€", decoded)
        if match_price:
            try:
                price_cents = round(float(match_price.group(1).replace(",", ".")) * 100)
            except ValueError:
                price_cents = 0

    return title, condition, price_cents


def is_valid_item_photo_url(url: str | None) -> bool:
    """Vérifie qu'une URL d'image correspond bien au produit et non à l'avatar du vendeur."""
    if not url or not url.startswith("http"):
        return False
    if "data:image" in url:
        return False
    lower = url.lower()
    avatar_keywords = [
        "/users/",
        "/avatars/",
        "user_avatar",
        "user_photo",
        "default_avatar",
        "profile",
        "/member/",
    ]
    return not any(kw in lower for kw in avatar_keywords)


def extract_all_catalog_photos(html_content: str) -> dict[str, str]:
    """Extrait en une passe toutes les photos associées aux annonces du catalogue."""
    images: dict[str, str] = {}
    for m in re.finditer(
        r'<img[^>]+src=["\']([^"\']+)["\'][^>]*data-testid=["\']product-item-id-(\d+)--image--img["\']',
        html_content,
    ):
        url = m.group(1)
        if is_valid_item_photo_url(url):
            images[m.group(2)] = url

    for m in re.finditer(
        r'<img[^>]+data-testid=["\']product-item-id-(\d+)--image--img["\'][^>]*src=["\']([^"\']+)["\']',
        html_content,
    ):
        url = m.group(2)
        if is_valid_item_photo_url(url):
            images[m.group(1)] = url

    return images


def extract_photo_url(html_content: str, item_id: str) -> str | None:
    """Extrait l'URL de la photo associée à l'item ID dans le DOM en ignorant les avatars."""
    m_direct = re.search(
        rf'<img[^>]+src=["\']([^"\']+)["\'][^>]*data-testid=["\']product-item-id-{item_id}--image--img["\']|'
        rf'<img[^>]+data-testid=["\']product-item-id-{item_id}--image--img["\'][^>]*src=["\']([^"\']+)["\']',
        html_content,
    )
    if m_direct:
        url = m_direct.group(1) or m_direct.group(2)
        if is_valid_item_photo_url(url):
            return url

    pattern = re.compile(
        rf'data-testid=["\']product-item-id-{item_id}(?:--image)?["\']'
        r'(?:(?!data-testid=["\']product-item-id-).)*',
        re.DOTALL,
    )
    block_match = pattern.search(html_content)
    if block_match:
        block_text = block_match.group(0)
        for img_match in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', block_text):
            candidate_url = img_match.group(1)
            if is_valid_item_photo_url(candidate_url):
                return candidate_url
    return None


def extract_item_page_photos(html_content: str) -> list[str]:
    """Extrait toutes les photos HD réelles d'une annonce Vinted sans avatar vendeur."""
    norm = html_content.replace(r"\"", '"').replace(r"\/", "/")
    found = re.findall(r'https:(?:[\\/])+images1\.vinted\.net[^\s"\'<>]+', norm)
    cleaned: list[str] = []
    seen: set[str] = set()

    for u in found:
        norm_url = re.sub(r"^https:[\\/]+", "https://", u).rstrip("\\")
        if "/f800/" not in norm_url or not is_valid_item_photo_url(norm_url):
            continue

        # Identifier le hash ou nom de fichier de l'image
        m = re.search(r"/f800/([^/?#.]+)\.", norm_url)
        if not m:
            continue
        img_hash = m.group(1)

        if img_hash in seen:
            continue

        # Vérifier dans le HTML source si cette image est un avatar (height:0, user.photo)
        idx = norm.find(img_hash)
        if idx != -1:
            preceding = norm[max(0, idx - 250) : idx]
            if '"height":0' in preceding or '"height": 0' in preceding:
                continue
            if '"photo":{' in preceding or '"photo": {' in preceding:
                continue

        seen.add(img_hash)
        cleaned.append(norm_url)

    return cleaned


def parse_vinted_catalog_html(html_content: str) -> list[ScrapedVintedItem]:
    """Extrait toutes les annonces visibles sur une page de catalogue Vinted."""
    items: list[ScrapedVintedItem] = []
    seen_in_page: set[str] = set()
    catalog_photos = extract_all_catalog_photos(html_content)

    for match in OVERLAY_LINK_REGEX.finditer(html_content):
        item_id = match.group(1)
        raw_title = match.group(2)

        if item_id in seen_in_page:
            continue
        seen_in_page.add(item_id)

        title, condition, price_cents = parse_vinted_overlay_title(raw_title)
        if not title or price_cents <= 0:
            continue

        photo_url = catalog_photos.get(item_id) or extract_photo_url(html_content, item_id)
        item_url = f"https://www.vinted.fr/items/{item_id}"

        items.append(
            ScrapedVintedItem(
                external_id=item_id,
                title=title,
                condition=condition,
                item_price_cents=price_cents,
                url=item_url,
                photo_url=photo_url,
            )
        )

    return items


def catalog_url_to_api_params(catalog_url: str) -> dict[str, str]:
    """Convertit une URL de catalogue ou de recherche Vinted en paramètres d'API /svc-catalogue."""
    parsed = urlparse(catalog_url)
    qs = parse_qs(parsed.query)
    params: dict[str, str] = {}
    for k, v in qs.items():
        clean_k = k.replace("[]", "").strip()
        val = ",".join(v).strip()
        if not val:
            continue
        if clean_k in ("catalog", "catalog_ids"):
            params["attribute_ids[catalog]"] = val
        elif clean_k in ("status_ids", "status"):
            params["attribute_ids[status]"] = val
        elif clean_k in ("language_book_ids", "language_book"):
            params["attribute_ids[language_book]"] = val
        elif clean_k in ("brand_ids", "brand"):
            params["attribute_ids[brand]"] = val
        elif clean_k in ("size_ids", "size"):
            params["attribute_ids[size]"] = val
        elif clean_k in ("search_text", "order", "price_from", "price_to"):
            params[clean_k] = v[0]

    params.setdefault("order", "newest_first")
    params.setdefault("per_page", "24")
    return params


def parse_vinted_catalog_json(data: dict[str, Any]) -> list[ScrapedVintedItem]:
    """Parse la réponse JSON structurée de l'API interne Vinted (/svc-catalogue/items)."""
    items: list[ScrapedVintedItem] = []
    raw_items = data.get("items") or []

    for raw in raw_items:
        item_id = str(raw.get("id") or "").strip()
        title = html.unescape(str(raw.get("title") or "")).strip()
        if not item_id or not title:
            continue

        # Extraction du prix en centimes
        price_cents = 0
        price_data = raw.get("price") or {}
        price_amount = price_data.get("amount")
        if price_amount is not None:
            try:
                price_cents = round(float(price_amount) * 100)
            except (ValueError, TypeError):
                price_cents = 0

        if price_cents <= 0:
            continue

        # Extraction de l'état (condition)
        item_box = raw.get("item_box") or {}
        second_line = str(item_box.get("second_line") or "").strip()
        condition = "Très bon état"
        if second_line:
            condition = second_line.split("·")[-1].strip() or "Très bon état"
        elif "accessibility_label" in item_box:
            acc_label = str(item_box.get("accessibility_label") or "")
            if ", État: " in acc_label:
                _, rest = acc_label.split(", État: ", 1)
                condition = rest.split(",")[0].strip() or "Très bon état"

        # URL de l'annonce
        raw_url = str(raw.get("url") or "").strip()
        if raw_url.startswith("http"):
            item_url = raw_url
        elif raw_url.startswith("/"):
            item_url = f"https://www.vinted.fr{raw_url}"
        else:
            item_url = f"https://www.vinted.fr/items/{item_id}"

        # Photo principale et photos supplémentaires HD
        photo_url = None
        extra_photos: list[str] = []
        seen_photos: set[str] = set()

        main_photo = raw.get("photo") or {}
        if main_photo.get("url") and is_valid_item_photo_url(main_photo["url"]):
            photo_url = main_photo["url"]
            seen_photos.add(photo_url)

        for p in raw.get("photos") or []:
            p_url = p.get("url")
            if p_url and is_valid_item_photo_url(p_url) and p_url not in seen_photos:
                if not photo_url:
                    photo_url = p_url
                else:
                    extra_photos.append(p_url)
                seen_photos.add(p_url)

        items.append(
            ScrapedVintedItem(
                external_id=item_id,
                title=title,
                condition=condition,
                item_price_cents=price_cents,
                url=item_url,
                photo_url=photo_url,
                extra_photos=extra_photos,
            )
        )

    return items


