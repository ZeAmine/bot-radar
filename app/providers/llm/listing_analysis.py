"""Analyse structurée d'une annonce : validation Pydantic, cache Redis, prompts versionnés.

Ce chemin est distinct du sous-système `app/services/ai/` : il rend un schéma strict
(`ListingAnalysisResult`) et mémoïse en Redis, là où les modules de conseil rendent
un JSON libre. Il ignore totalement le moteur : changer de modèle ne change rien ici.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Final

import httpx
from pydantic import ValidationError
from redis.asyncio import Redis

from app.core.config import settings
from app.core.exceptions import LLMAPIError, LLMTimeoutError, LLMValidationError
from app.core.redis import get_redis_client
from app.domain.entities.analysis import ListingAnalysisResult
from app.domain.enums import CategorySlug
from app.domain.protocols.llm import LLMAnalysisProviderProtocol
from app.providers.llm.base import LLMClient, clean_and_parse_json
from app.providers.llm.registry import llm_client as default_llm_client

logger = logging.getLogger("radar.providers.llm")

PROMPTS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "prompts" / "v1"
CACHE_TTL_SECONDS: Final[int] = 86400 * 7  # 7 jours
ANALYSIS_TEMPERATURE: Final[float] = 0.1
PROMPT_VERSION: Final[str] = "v1"


class ListingAnalysisProvider(LLMAnalysisProviderProtocol):
    """Analyse d'annonce validée par schéma, mémoïsée, avec repli gracieux."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        redis_client: Redis | None = None,
        timeout_seconds: float = 6.0,
        raise_exceptions: bool = False,
    ) -> None:
        self._llm = llm if llm is not None else default_llm_client
        self._redis = redis_client
        self._timeout = timeout_seconds
        self._raise_exceptions = raise_exceptions
        self._system_prompt = self._load_prompt("listing_analysis_system.txt")
        self._user_prompt_template = self._load_prompt("listing_analysis_user.txt")

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
        """Clé de cache déterministe, qui tient compte du moteur et du prompt.

        Le fournisseur, le modèle et la version de prompt font partie de la clé : sans
        eux, un changement de moteur continuerait de servir les analyses de l'ancien
        pendant sept jours, en silence.
        """
        identity = f"{self._llm.vendor}:{self._llm.model}:{PROMPT_VERSION}"
        raw_key = (
            f"{identity}:{category.value}:{title.strip().lower()}:{description.strip().lower()}"
        )
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return f"radar:llm:analysis:{digest}"

    async def analyze_listing(
        self,
        title: str,
        description: str,
        price_cents: int,
        declared_condition: str | None,
        category: CategorySlug,
        image_urls: list[str] | None = None,
    ) -> ListingAnalysisResult | None:
        """Analyse une annonce avec validation Pydantic, cache Redis et repli gracieux.

        En cas d'erreur ou de timeout, retourne None (ou lève l'exception si
        raise_exceptions=True).
        """
        if not settings.AI_ENABLED:
            logger.debug("Analyse IA ignorée : AI_ENABLED=False")
            return None
        if not self._llm.is_configured:
            logger.debug("Analyse IA ignorée : clé API non configurée")
            return None

        cache_key = self.compute_cache_key(title, description, category)

        # 1. Vérification du cache Redis
        try:
            cached_payload = await self.redis.get(cache_key)
            if cached_payload:
                logger.info("Cache d'analyse LLM HIT pour la clé %s", cache_key)
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

        # 3. Appel unique au moteur, même pile HTTP que le post-alerte
        try:
            raw_text = await self._llm.complete(
                system_prompt=self._system_prompt,
                user_prompt=user_prompt,
                temperature=ANALYSIS_TEMPERATURE,
                image_urls=image_urls,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            logger.warning(
                "Timeout (%ss) dépassé lors de l'analyse pour '%s'",
                self._timeout,
                title[:40],
            )
            if self._raise_exceptions:
                raise LLMTimeoutError("Timeout dépassé lors de l'appel au moteur LLM") from exc
            return None
        except Exception as exc:
            logger.exception("Erreur inattendue lors de l'analyse LLM : %s", exc)
            if self._raise_exceptions:
                raise LLMAPIError("Erreur réseau inattendue") from exc
            return None

        if not raw_text:
            logger.warning("Analyse LLM vide pour '%s'", title[:40])
            return None

        # 4. Parsing et validation Pydantic stricte
        payload = clean_and_parse_json(raw_text)
        if payload is None:
            if self._raise_exceptions:
                raise LLMValidationError("Structure JSON invalide")
            return None
        try:
            result = ListingAnalysisResult.model_validate(payload)
        except ValidationError as val_err:
            logger.error("Validation Pydantic échouée sur la réponse LLM : %s", val_err)
            if self._raise_exceptions:
                raise LLMValidationError("Structure JSON invalide") from val_err
            return None

        # 5. Persistance dans le cache Redis
        try:
            await self.redis.setex(cache_key, CACHE_TTL_SECONDS, result.model_dump_json())
        except Exception as cache_err:
            logger.warning("Erreur lors de l'écriture dans le cache Redis : %s", cache_err)

        return result
