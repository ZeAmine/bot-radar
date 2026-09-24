import asyncio
import base64
import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.config import settings
from app.core.http_client import get_shared_http_client
from app.core.redis import get_redis_client
from app.services.price_calculator import (
    DEFAULT_SHIPPING_CENTS,
    calculate_buyer_protection_cents,
)

logger = logging.getLogger("radar.vinted_scraper")

API_CATALOG_ENDPOINT = "https://api.vinted.fr/svc-catalogue/items"


class VintedBlockError(Exception):
    """Exception levée en cas de blocage Cloudflare/DataDome (403)."""

    def __init__(
        self, status_code: int = 403, message: str = "Blocage Cloudflare Vinted (403)"
    ) -> None:
        self.status_code = status_code
        super().__init__(message)


class VintedRateLimitError(Exception):
    """Exception levée en cas de limitation de débit Vinted (429)."""

    def __init__(self, status_code: int = 429, message: str = "Rate limit Vinted (429)") -> None:
        self.status_code = status_code
        super().__init__(message)


DEFAULT_VINTED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

DEFAULT_GLOBAL_BD_URL = (
    "https://www.vinted.fr/catalog?search_text=&catalog[]=5425"
    "&status_ids[]=1&status_ids[]=2&status_ids[]=6"
    "&language_book_ids[]=6436&search_id=2946920741&order=newest_first"
)

# Regex pour extraire l'overlay lien et titre
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


async def fetch_item_photos(
    item_url: str,
    http_client: httpx.AsyncClient | None = None,
) -> list[str]:
    """Récupère toutes les photos haute résolution d'une annonce depuis sa page détaillée."""
    headers = DEFAULT_VINTED_HEADERS
    try:
        if http_client:
            resp = await http_client.get(item_url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                return extract_item_page_photos(resp.text)
        elif not settings.get_vinted_proxy():
            client = get_shared_http_client()
            resp = await client.get(item_url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                return extract_item_page_photos(resp.text)
        else:
            async with httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=True,
                proxy=settings.get_vinted_proxy(),
            ) as client:
                resp = await client.get(item_url, headers=headers)
                if resp.status_code == 200:
                    return extract_item_page_photos(resp.text)
    except Exception as exc:
        logger.warning("Impossible de récupérer les photos de l'annonce %s : %s", item_url, exc)
    return []


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


REDIS_SESSION_KEY = "radar:vinted_session_cookies"
REDIS_SESSION_TTL = 18 * 3600  # 18 heures (les jetons Vinted expirent sous 24h)


def get_jwt_exp(token: str) -> float | None:
    """Extrait l'horodatage d'expiration 'exp' d'un token JWT sans dépendance externe."""
    try:
        parts = token.split(".")
        if len(parts) >= 2:
            padding = "=" * (4 - len(parts[1]) % 4)
            payload_bytes = base64.urlsafe_b64decode(parts[1] + padding)
            payload = json.loads(payload_bytes)
            if "exp" in payload:
                return float(payload["exp"])
    except Exception:
        pass
    return None


def is_session_refresh_page(html_content: str) -> bool:
    """Détecte si Vinted a retourné la page blanche 'Session refresh' au lieu du catalogue."""
    if not html_content:
        return True
    if "Session refresh" in html_content:
        return True
    return len(html_content) < 80000 and "product-item-id-" not in html_content


class VintedSessionManager:
    """Gestionnaire automatique de session Vinted avec cookies et persistance Redis."""

    def __init__(self, refresh_interval_seconds: int = 7200) -> None:
        self.refresh_interval = refresh_interval_seconds
        self.last_refreshed: float = 0
        self.last_attempt: float = 0
        self.cookies: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def _load_cookies_from_redis(self) -> dict[str, str]:
        """Tente de restaurer les cookies Vinted persistés dans Redis."""
        try:
            client = get_redis_client()
            raw_data = await client.get(REDIS_SESSION_KEY)
            if raw_data:
                data = json.loads(raw_data)
                if isinstance(data, dict) and data:
                    logger.info("Session Vinted restaurée depuis Redis (%d cookies).", len(data))
                    return data
        except Exception as exc:
            logger.debug("Impossible de charger les cookies Vinted depuis Redis: %s", exc)
        return {}

    async def _save_cookies_to_redis(self, cookies: dict[str, str]) -> None:
        """Sauvegarde les cookies de session Vinted dans Redis (TTL 18 heures)."""
        if not cookies:
            return
        try:
            client = get_redis_client()
            await client.set(REDIS_SESSION_KEY, json.dumps(cookies), ex=REDIS_SESSION_TTL)
            logger.info("Session Vinted persistée dans Redis (%d cookies, TTL 18h).", len(cookies))
        except Exception as exc:
            logger.debug("Impossible de persister les cookies Vinted dans Redis: %s", exc)

    async def set_manual_session(self, cookies: dict[str, str]) -> None:
        """Injecte manuellement des cookies de session dans le gestionnaire et dans Redis."""
        if not cookies:
            return
        async with self._lock:
            self.cookies = dict(cookies)
            self.last_refreshed = time.time()
            await self._save_cookies_to_redis(self.cookies)
            logger.info(
                "Nouvelle session Vinted manuelle enregistrée (%d cookies).",
                len(self.cookies),
            )

    async def refresh_session(self, client: httpx.AsyncClient) -> bool:
        """Visite la page d'accueil de Vinted pour récupérer des cookies et jetons frais."""
        now = time.time()
        if now - self.last_attempt < 30:
            return False
        async with self._lock:
            self.last_attempt = time.time()
            try:
                resp = await client.get(
                    "https://www.vinted.fr/",
                    headers=DEFAULT_VINTED_HEADERS,
                    follow_redirects=True,
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    if hasattr(client.cookies, "jar"):
                        for cookie in client.cookies.jar:
                            self.cookies[cookie.name] = cookie.value
                    else:
                        for k, v in client.cookies.items():
                            self.cookies[k] = v
                    self.last_refreshed = time.time()
                    await self._save_cookies_to_redis(self.cookies)
                    logger.info(
                        "Session Vinted rafraîchie avec succès (%d cookies actifs)",
                        len(self.cookies),
                    )
                    return True
                else:
                    logger.warning(
                        "Échec du rafraîchissement de session Vinted (statut %d)", resp.status_code
                    )
            except Exception as exc:
                logger.warning("Erreur lors du rafraîchissement de session Vinted: %s", exc)
            return False

    async def ensure_valid_session(self, client: httpx.AsyncClient, force: bool = False) -> None:
        """Vérifie si la session doit être rafraîchie et injecte les en-têtes nécessaires."""
        # 1. Si pas de cookies en mémoire vive, tenter de restaurer depuis Redis
        if not self.cookies:
            redis_cookies = await self._load_cookies_from_redis()
            if redis_cookies:
                self.cookies = redis_cookies
                self.last_refreshed = time.time()

        # 2. Injecter les cookies connus dans le client HTTP
        if hasattr(client.cookies, "set"):
            for k, v in self.cookies.items():
                client.cookies.set(k, v, domain=".vinted.fr")
        elif isinstance(client.cookies, dict):
            client.cookies.update(self.cookies)

        now = time.time()
        token = self.cookies.get("access_token_web")
        token_expired = False
        if token:
            exp = get_jwt_exp(token)
            if exp and exp <= now + 300:  # Expire dans moins de 5 min ou déjà expiré
                token_expired = True

        needs_refresh = (
            force
            or token_expired
            or (now - self.last_refreshed > self.refresh_interval)
            or not self.cookies
            or not token
        )
        if needs_refresh and (now - self.last_attempt > 30):
            await self.refresh_session(client)

    def clear_session(self) -> None:
        """Vide les cookies en mémoire et réinitialise les horodatages."""
        self.cookies.clear()
        self.last_refreshed = 0
        self.last_attempt = 0

    def get_auth_headers(self) -> dict[str, str]:
        """Retourne les en-têtes adaptés incluant le Bearer token si présent."""
        headers = dict(DEFAULT_VINTED_HEADERS)
        token = self.cookies.get("access_token_web")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def get_api_headers(self) -> dict[str, str]:
        """Retourne les en-têtes adaptés pour l'API interne JSON (/svc-catalogue/items)."""
        headers = {
            **DEFAULT_VINTED_HEADERS,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.vinted.fr",
            "Referer": "https://www.vinted.fr/",
            "Locale": "fr-FR",
            "Platform": "web",
            "X-Next-App": "marketplace-web",
        }
        token = self.cookies.get("access_token_web")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        anon_id = self.cookies.get("anon_id")
        if anon_id:
            headers["X-Anon-Id"] = anon_id
        return headers


vinted_session_manager = VintedSessionManager()


async def fetch_vinted_page(
    url: str,
    client: httpx.AsyncClient,
) -> list[ScrapedVintedItem]:
    """Télécharge une page de catalogue Vinted et extrait les annonces.

    Interroge en priorité l'API interne JSON (/svc-catalogue/items) ultra-rapide.
    Si l'API interne retourne une réponse inattendue ou échoue, bascule automatiquement
    et sans interruption sur le scraping du catalogue HTML classique.
    """
    try:
        await vinted_session_manager.ensure_valid_session(client)

        # 1. Tentative API Interne JSON (/svc-catalogue/items)
        api_params = catalog_url_to_api_params(url)
        api_headers = vinted_session_manager.get_api_headers()

        response = await client.get(
            API_CATALOG_ENDPOINT,
            params=api_params,
            headers=api_headers,
        )

        if response.status_code == 401:
            logger.info("Vinted API 401, renouvellement de session en cours...")
            if await vinted_session_manager.refresh_session(client):
                api_headers = vinted_session_manager.get_api_headers()
                response = await client.get(
                    API_CATALOG_ENDPOINT,
                    params=api_params,
                    headers=api_headers,
                )

        if response.status_code == 200:
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                try:
                    data = response.json()
                    items = parse_vinted_catalog_json(data)
                    if items or not is_session_refresh_page(response.text):
                        return items
                except Exception as parse_err:
                    logger.debug("Erreur parsing JSON Vinted (%s), repli HTML: %s", url, parse_err)
            else:
                logger.debug("Réponse non-JSON reçue sur l'API Vinted, repli HTML...")

        elif response.status_code == 429:
            logger.warning("Vinted rate limit (429) sur l'API: %s", url)
            raise VintedRateLimitError(429, f"Vinted rate limit (429) sur {url}")
        elif response.status_code == 403:
            logger.warning("Vinted blocage Cloudflare/DataDome (403) sur l'API: %s", url)
            raise VintedBlockError(403, f"Vinted blocage Cloudflare (403) sur {url}")
        else:
            logger.debug(
                "Vinted API statut %d pour %s, bascule vers catalogue HTML...",
                response.status_code,
                url,
            )

        # 2. Secours : Scraping du catalogue HTML classique
        headers = vinted_session_manager.get_auth_headers()
        html_response = await client.get(url, headers=headers)

        is_refresh_page = (
            html_response.status_code == 200
            and is_session_refresh_page(html_response.text)
        )

        if html_response.status_code in (401, 403) or is_refresh_page:
            reason = (
                f"statut {html_response.status_code}"
                if html_response.status_code != 200
                else "page d'attente 'Session refresh'"
            )
            logger.info(
                "Vinted HTML %s, tentative de renouvellement automatique de session...",
                reason,
            )
            if await vinted_session_manager.refresh_session(client):
                headers = vinted_session_manager.get_auth_headers()
                html_response = await client.get(url, headers=headers)

        if html_response.status_code == 200:
            items = parse_vinted_catalog_html(html_response.text)
            if not items and is_session_refresh_page(html_response.text):
                logger.warning(
                    "Vinted catalogue non authentifié reçu, purge de session pour le prochain tour."
                )
                vinted_session_manager.clear_session()
            return items
        if html_response.status_code == 429:
            logger.warning("Vinted rate limit (429) sur le catalogue HTML: %s", url)
            raise VintedRateLimitError(429, f"Vinted rate limit (429) sur {url}")
        elif html_response.status_code == 403:
            logger.warning("Vinted blocage Cloudflare (403) sur le catalogue HTML: %s", url)
            raise VintedBlockError(403, f"Vinted blocage Cloudflare (403) sur {url}")
        else:
            logger.warning(
                "Vinted statut HTTP inattendu %s pour: %s",
                html_response.status_code,
                url,
            )
        return []

    except (VintedBlockError, VintedRateLimitError):
        raise
    except Exception as exc:
        logger.debug("Erreur lors de la requête Vinted (%s): %s", url, exc)
        return []


def to_ingest_payload(
    item: ScrapedVintedItem,
    category_slug: str = "bd_comics",
    isbn13: str | None = None,
) -> dict[str, Any]:
    """Convertit une annonce extraite en payload pour l'API d'ingestion."""
    # Calcul des frais standards Vinted
    protection_cents = calculate_buyer_protection_cents(item.item_price_cents)
    shipping_cents = DEFAULT_SHIPPING_CENTS

    all_photos: list[str] = []
    if item.photo_url:
        all_photos.append(item.photo_url)
    for p in getattr(item, "extra_photos", []):
        if p and p not in all_photos:
            all_photos.append(p)

    payload: dict[str, Any] = {
        "source_name": "vinted",
        "external_id": item.external_id,
        "url": item.url,
        "title": item.title,
        "description": f"Exemplaire {item.title} en état {item.condition}.",
        "category_slug": category_slug,
        "item_price_cents": item.item_price_cents,
        "estimated_protection_cents": protection_cents,
        "estimated_shipping_cents": shipping_cents,
        "condition": item.condition,
        "seller_name": "vendeur_vinted",
        "seller_rating": 4.9,
        "photos": all_photos,
        "isbn13": isbn13,
    }
    return payload
