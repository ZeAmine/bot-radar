"""Fournisseur d'analyse IA DeepSeek conforme au protocole LLMAnalysisProviderProtocol."""

import hashlib
import logging
from pathlib import Path
from typing import Any, Final

import httpx
from pydantic import ValidationError
from redis.asyncio import Redis

from app.core.config import settings
from app.core.exceptions import (
    DeepSeekAPIError,
    DeepSeekTimeoutError,
    DeepSeekValidationError,
)
from app.core.http_client import get_shared_http_client
from app.core.redis import get_redis_client
from app.domain.entities.analysis import ListingAnalysisResult
from app.domain.enums import CategorySlug
from app.domain.protocols.llm import LLMAnalysisProviderProtocol

logger = logging.getLogger("radar.providers.deepseek")

PROMPTS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "prompts" / "v1"
CACHE_TTL_SECONDS: Final[int] = 86400 * 7  # 7 jours


class DeepSeekProvider(LLMAnalysisProviderProtocol):
    """Client asynchrone DeepSeek avec cache Redis, prompts versionnés et validation Pydantic."""

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        redis_client: Redis | None = None,
        timeout_seconds: float = 6.0,
        raise_exceptions: bool = False,
    ) -> None:
        self._http = http_client
        self._redis = redis_client
        self._timeout = timeout_seconds
        self._raise_exceptions = raise_exceptions
        self._system_prompt = self._load_prompt("listing_analysis_system.txt")
        self._user_prompt_template = self._load_prompt("listing_analysis_user.txt")

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Résout le client HTTP partagé si non injecté."""
        if self._http is None:
            self._http = get_shared_http_client()
        return self._http

    @property
    def redis(self) -> Redis:
        """Résout le client Redis partagé si non injecté."""
        if self._redis is None:
            self._redis = get_redis_client()
        return self._redis

    @staticmethod
    def _load_prompt(filename: str) -> str:
        prompt_path = PROMPTS_DIR / filename
        if not prompt_path.is_file():
            raise FileNotFoundError(f"Fichier de prompt introuvable : {prompt_path}")
        return prompt_path.read_text(encoding="utf-8").strip()

    def compute_cache_key(
        self,
        title: str,
        description: str,
        category: CategorySlug,
    ) -> str:
        """Calcule une clé de cache SHA-256 déterministe pour l'annonce."""
        raw_key = f"v1:{category.value}:{title.strip().lower()}:{description.strip().lower()}"
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return f"radar:deepseek:analysis:{digest}"

    async def analyze_listing(
        self,
        title: str,
        description: str,
        price_cents: int,
        declared_condition: str | None,
        category: CategorySlug,
        image_urls: list[str] | None = None,
    ) -> ListingAnalysisResult | None:
        """Analyse une annonce avec validation Pydantic, cache Redis et fallback gracieux.

        En cas d'erreur ou de timeout, retourne None (ou lève l'exception si raise_exceptions=True).
        """
        if not settings.AI_ENABLED:
            logger.debug("Analyse IA ignorée : AI_ENABLED=False")
            return None

        api_key = settings.DEEPSEEK_API_KEY.get_secret_value().strip()
        if not api_key:
            logger.debug("Analyse IA ignorée : clé API non configurée")
            return None

        cache_key = self.compute_cache_key(title, description, category)

        # 1. Vérification du cache Redis
        try:
            cached_payload = await self.redis.get(cache_key)
            if cached_payload:
                logger.info("DeepSeek Cache HIT pour la clé %s", cache_key)
                return ListingAnalysisResult.model_validate_json(cached_payload)
        except Exception as cache_err:
            logger.warning("Erreur lors de la lecture du cache Redis : %s", cache_err)

        # 2. Construction de la requête
        user_prompt = self._user_prompt_template.format(
            title=title,
            category=category.value,
            price=f"{price_cents / 100:.2f} €",
            condition=declared_condition or "Non précisé",
            description=description or "Aucune description",
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
        ]

        if image_urls:
            content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
            for url in image_urls[:5]:
                if url and url.startswith("http"):
                    content.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user_prompt})

        payload = {
            "model": settings.DEEPSEEK_MODEL,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        endpoint = f"{settings.DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"

        # 3. Appel asynchrone via httpx
        try:
            response = await self.http_client.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            logger.warning(
                "Timeout (%ss) dépassé lors de l'appel à DeepSeek pour '%s'",
                self._timeout,
                title[:40],
            )
            if self._raise_exceptions:
                raise DeepSeekTimeoutError("Timeout dépassé lors de l'appel à DeepSeek") from exc
            return None
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Erreur HTTP %s DeepSeek pour '%s' : %s",
                exc.response.status_code,
                title[:40],
                exc.response.text[:200],
            )
            if self._raise_exceptions:
                raise DeepSeekAPIError(
                    f"Erreur HTTP {exc.response.status_code} DeepSeek",
                    status_code=exc.response.status_code,
                ) from exc
            return None
        except Exception as exc:
            logger.exception("Erreur inattendue lors de l'appel à DeepSeek : %s", exc)
            if self._raise_exceptions:
                raise DeepSeekAPIError("Erreur réseau inattendue") from exc
            return None

        # 4. Parsing et validation Pydantic stricte
        choices = data.get("choices", [])
        if not choices:
            logger.warning("Réponse DeepSeek invalide ou vide : %s", data)
            return None

        raw_content = choices[0].get("message", {}).get("content", "").strip()
        try:
            result = ListingAnalysisResult.model_validate_json(raw_content)
        except ValidationError as val_err:
            logger.error("Validation Pydantic échouée sur la réponse DeepSeek : %s", val_err)
            if self._raise_exceptions:
                raise DeepSeekValidationError("Structure JSON invalide") from val_err
            return None

        # 5. Persistance dans le cache Redis
        try:
            await self.redis.setex(
                cache_key,
                CACHE_TTL_SECONDS,
                result.model_dump_json(),
            )
        except Exception as cache_err:
            logger.warning("Erreur lors de l'écriture dans le cache Redis : %s", cache_err)

        return result
