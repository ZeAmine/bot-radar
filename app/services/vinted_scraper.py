import asyncio
import base64
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
import redis.asyncio as aioredis

from app.api.dependencies import get_redis_pool
from app.core.config import settings

logger = logging.getLogger("radar.vinted_scraper")


class VintedBlockError(Exception):
    """Exception levée en cas de blocage Cloudflare/DataDome (403)."""

    def __init__(self, status_code: int = 403, message: str = "Blocage Cloudflare Vinted (403)") -> None:
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
    """Extrait toutes les photos haute résolution réelles d'une annonce Vinted sans avatar vendeur."""
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
    """Gestionnaire automatique de session Vinted avec auto-renouvellement des cookies et persistance Redis."""

    def __init__(self, refresh_interval_seconds: int = 7200) -> None:
        self.refresh_interval = refresh_interval_seconds
        self.last_refreshed: float = 0
        self.last_attempt: float = 0
        self.cookies: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def _load_cookies_from_redis(self) -> dict[str, str]:
        """Tente de restaurer les cookies Vinted persistés dans Redis."""
        try:
            pool = get_redis_pool()
            client = aioredis.Redis(connection_pool=pool)
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
            pool = get_redis_pool()
            client = aioredis.Redis(connection_pool=pool)
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
            logger.info("Nouvelle session Vinted manuelle enregistrée (%d cookies).", len(self.cookies))

    async def refresh_session(self, client: httpx.AsyncClient) -> bool:
        """Effectue une visite sur la page d'accueil de Vinted pour récupérer des cookies et jetons frais."""
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
        """Vide les cookies en mémoire et réinitialise les horodatages pour forcer une négociation propre."""
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


vinted_session_manager = VintedSessionManager()


async def fetch_vinted_page(
    url: str,
    client: httpx.AsyncClient,
) -> list[ScrapedVintedItem]:
    """Télécharge une page de catalogue Vinted et extrait les annonces."""
    try:
        await vinted_session_manager.ensure_valid_session(client)
        headers = vinted_session_manager.get_auth_headers()
        response = await client.get(url, headers=headers)

        is_refresh_page = response.status_code == 200 and is_session_refresh_page(response.text)

        if response.status_code in (401, 403) or is_refresh_page:
            reason = (
                f"statut {response.status_code}"
                if response.status_code != 200
                else "page d'attente 'Session refresh'"
            )
            logger.info(
                "Vinted %s, tentative de renouvellement automatique de session...",
                reason,
            )
            if await vinted_session_manager.refresh_session(client):
                headers = vinted_session_manager.get_auth_headers()
                response = await client.get(url, headers=headers)

        if response.status_code == 200:
            items = parse_vinted_catalog_html(response.text)
            if not items and is_session_refresh_page(response.text):
                logger.warning(
                    "Vinted catalogue non authentifié reçu, purge de session pour le prochain tour."
                )
                vinted_session_manager.clear_session()
            return items
        if response.status_code == 429:
            logger.warning("Vinted rate limit (429) sur l'URL: %s", url)
            raise VintedRateLimitError(429, f"Vinted rate limit (429) sur {url}")
        elif response.status_code == 403:
            logger.warning("Vinted blocage Cloudflare/DataDome (403) sur: %s", url)
            raise VintedBlockError(403, f"Vinted blocage Cloudflare (403) sur {url}")
        else:
            logger.warning("Vinted statut HTTP inattendu %s pour: %s", response.status_code, url)
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
    protection_cents = 70 + int(item.item_price_cents * 0.05)
    shipping_cents = 299

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
        "photos": [item.photo_url] if item.photo_url else [],
        "isbn13": isbn13,
    }
    return payload
