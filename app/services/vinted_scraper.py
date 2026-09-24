import asyncio
import base64
import json
import logging
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.http_client import get_shared_http_client
from app.core.redis import get_redis_client
from app.services.price_calculator import (
    DEFAULT_SHIPPING_CENTS,
    calculate_buyer_protection_cents,
)
from app.services.vinted_parsing import (
    ScrapedVintedItem,
    catalog_url_to_api_params,
    extract_all_catalog_photos,
    extract_item_page_photos,
    extract_photo_url,
    parse_vinted_catalog_html,
    parse_vinted_catalog_json,
    parse_vinted_overlay_title,
)

__all__ = [
    "ScrapedVintedItem",
    "catalog_url_to_api_params",
    "extract_all_catalog_photos",
    "extract_item_page_photos",
    "extract_photo_url",
    "parse_vinted_catalog_html",
    "parse_vinted_catalog_json",
    "parse_vinted_overlay_title",
]

logger = logging.getLogger("radar.vinted_scraper")


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
