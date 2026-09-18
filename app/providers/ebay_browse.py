"""Fournisseur de prix officiel eBay Browse API."""

import base64
import logging
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.constants import MatchConfidence
from app.core.http_client import get_shared_http_client
from app.providers.base import MarketOffer

logger = logging.getLogger(__name__)


class EbayBrowseProvider:
    """Connecteur officiel vers l'API eBay Browse via OAuth2 Client Credentials."""

    name: str = "ebay"

    @classmethod
    def is_configured(cls) -> bool:
        """Vérifie si les identifiants eBay sont réellement renseignés et valides (non placeholder)."""
        client_id = settings.EBAY_CLIENT_ID or ""
        client_secret = settings.EBAY_CLIENT_SECRET.get_secret_value() or ""
        if not client_id or not client_secret:
            return False
        return not ("your_ebay" in client_id.lower() or "your_ebay" in client_secret.lower())

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self.http_client = http_client
        self.is_sandbox = settings.EBAY_ENV.upper() == "SANDBOX"
        self.auth_url = (
            "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
            if self.is_sandbox
            else "https://api.ebay.com/identity/v1/oauth2/token"
        )
        self.api_base_url = (
            "https://api.sandbox.ebay.com/buy/browse/v1"
            if self.is_sandbox
            else "https://api.ebay.com/buy/browse/v1"
        )
        self._cached_token: str | None = None
        self._token_expires_at: float = 0.0
        self._auth_failed_until: float = 0.0

    async def _get_access_token(self, client: httpx.AsyncClient) -> str | None:
        """Obtient ou réutilise un token OAuth client credentials."""
        now = time.time()
        if not self.is_configured():
            return None

        if now < self._auth_failed_until:
            return None

        if self._cached_token and now < self._token_expires_at - 60:
            return self._cached_token

        client_id = settings.EBAY_CLIENT_ID
        client_secret = settings.EBAY_CLIENT_SECRET.get_secret_value()

        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers = {
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        }

        try:
            response = await client.post(self.auth_url, headers=headers, data=data, timeout=5.0)
            if response.status_code != 200:
                self._auth_failed_until = now + 3600.0  # Suspend les requêtes eBay pendant 1h
                logger.warning(
                    "Échec d'authentification OAuth eBay (HTTP %s). Suspension temporaire d'eBay pendant 1 heure.",
                    response.status_code,
                )
                return None

            payload = response.json()
            self._cached_token = payload.get("access_token")
            expires_in = payload.get("expires_in", 7200)
            self._token_expires_at = now + float(expires_in)
            return self._cached_token
        except Exception as exc:
            self._auth_failed_until = now + 300.0
            logger.warning("Erreur réseau lors de la récupération du token eBay: %s", exc)
            return None

    async def get_offers(
        self,
        *,
        isbn13: str | None,
        title: str,
        author: str | None = None,
        edition: str | None = None,
    ) -> list[MarketOffer]:
        """Recherche les offres d'occasion sur eBay pour un livre donné."""
        if not self.is_configured():
            return []

        client = self.http_client or get_shared_http_client()
        token = await self._get_access_token(client)
        if not token:
            return []

        # Priorité absolue à l'ISBN-13 (GTIN), sinon requête textuelle combinée
        confidence = MatchConfidence.HIGH
        if isbn13 and len(isbn13.strip()) >= 10:
            query = f"gtin:{isbn13.strip()}"
        else:
            parts = [title]
            if author:
                parts.append(author)
            if edition:
                parts.append(edition)
            query = " ".join(parts)
            confidence = MatchConfidence.MEDIUM if author else MatchConfidence.LOW

        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_FR",
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Content-Type": "application/json",
        }

        params: dict[str, Any] = {
            "q": query,
            "limit": "10",
            "filter": "buyingOptions:{FIXED_PRICE},conditions:{USED}",
        }

        search_url = f"{self.api_base_url}/item_summary/search"
        try:
            response = await client.get(search_url, headers=headers, params=params, timeout=5.0)
            if response.status_code != 200:
                logger.warning("Erreur recherche eBay (HTTP %s)", response.status_code)
                return []

            data = response.json()
            items = data.get("itemSummaries", [])
            return self._parse_items(items, confidence)
        except Exception as exc:
            logger.warning("Erreur lors de l'appel eBay Browse API: %s", exc)
            return []

    def _parse_items(
        self, items: list[dict[str, Any]], confidence: MatchConfidence
    ) -> list[MarketOffer]:
        """Convertit les items bruts eBay en offres de marché strictement comparables."""
        offers: list[MarketOffer] = []

        for item in items:
            # 1. Vérification du prix article
            price_info = item.get("price", {})
            currency = price_info.get("currency")
            if currency != "EUR":
                continue

            try:
                item_price_float = float(price_info.get("value", 0))
                item_price_cents = round(item_price_float * 100)
            except (ValueError, TypeError):
                continue

            if item_price_cents <= 0:
                continue

            # 2. Vérification obligatoire des frais de livraison (ne jamais comparer si inconnu)
            shipping_options = item.get("shippingOptions", [])
            if not shipping_options:
                continue

            shipping_cost_info = shipping_options[0].get("shippingCost", {})
            shipping_currency = shipping_cost_info.get("currency", "EUR")
            if shipping_currency != "EUR":
                continue

            try:
                shipping_val = shipping_cost_info.get("value", 0.0)
                shipping_price_cents = round(float(shipping_val) * 100)
            except (ValueError, TypeError):
                continue

            delivered_price_cents = item_price_cents + shipping_price_cents
            source_url = item.get("itemWebUrl") or ""

            offers.append(
                MarketOffer(
                    provider_name=self.name,
                    item_price_cents=item_price_cents,
                    shipping_price_cents=shipping_price_cents,
                    delivered_price_cents=delivered_price_cents,
                    source_url=source_url,
                    currency="EUR",
                    item_condition=item.get("condition"),
                    confidence=confidence,
                    is_available=True,
                    is_comparable=True,
                    raw_payload=item,
                )
            )

        return offers
