"""Contrat du moteur LLM : la seule frontière qui connaît un fournisseur.

Ce module ne nomme ni fournisseur, ni modèle, ni format de filaire. Tout ce qui est
au-dessus — les modules de `app/services/ai/`, le pipeline post-alerte, `/health`,
`/status` — parle à `LLMClient` et n'a pas à changer quand le modèle change.
Changer de moteur est l'affaire de `registry.py` et d'un adaptateur.

La répartition est nette : `BaseLLMClient` porte la sémantique commune — activation,
client HTTP partagé, traitement des erreurs, repli `None`, sonde de santé — et
l'adaptateur ne traduit que le filaire de son fournisseur. Un adaptateur écrit cinq
méthodes, pas une seconde implémentation du client.
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict

from app.core.config import settings
from app.core.http_client import get_shared_http_client

logger = logging.getLogger(__name__)

# Statuts possibles, repris tels quels par `/health` et `/status`.
STATUS_ACTIVE = "active"
STATUS_UNCONFIGURED = "unconfigured"
STATUS_DISABLED = "disabled"
STATUS_ERROR = "error"

HEALTH_CACHE_SECONDS = 30.0
HEALTH_TIMEOUT_SECONDS = 4.0


class LLMHealth(BaseModel):
    """État opérationnel d'un moteur, indépendant du fournisseur.

    Sérialisé tel quel par `/health` : la forme publique ne bouge pas quand le
    moteur change.
    """

    model_config = ConfigDict(frozen=True)

    status: str
    model: str
    latency_ms: int | None = None
    message: str = ""

    def as_dict(self) -> dict[str, object]:
        """Retourne la forme JSON publique, telle que `/health` l'exposait déjà."""
        return {
            "status": self.status,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "message": self.message,
        }


@runtime_checkable
class LLMClient(Protocol):
    """Ce que le projet attend d'un moteur IA, quel qu'il soit."""

    @property
    def vendor(self) -> str:
        """Identifiant du fournisseur : clé de cache et préfixe de logs."""
        ...

    @property
    def model(self) -> str:
        """Nom du modèle effectivement appelé."""
        ...

    @property
    def is_configured(self) -> bool:
        """Vrai si le moteur peut être appelé sans erreur de configuration."""
        ...

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        image_urls: list[str] | None = None,
        timeout: float | None = None,
    ) -> str | None:
        """Envoie un prompt et rend le texte produit, ou ``None`` si l'appel échoue.

        ``None`` est le repli attendu : le bot dégrade vers ses algorithmes, il ne
        lève jamais. ``image_urls`` demande une analyse multimodale et n'est
        supporté que par les adaptateurs qui le déclarent.
        """
        ...

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> dict[str, object] | None:
        """Rend un objet JSON, ou ``None`` si la réponse est vide, invalide ou l'appel a échoué."""
        ...

    async def check_health(self, force: bool = False) -> LLMHealth:
        """Sonde le moteur, avec cache court pour ne pas frapper à chaque `/status`."""
        ...


class BaseLLMClient(ABC):
    """Sémantique commune d'un moteur : configuration, appels, erreurs, santé.

    Un adaptateur fournit cinq méthodes de traduction — `chat_endpoint`,
    `chat_headers`, `build_payload`, `extract_text`, `health_request` — et rien de
    plus.
    """

    vendor: str = "unknown"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout_seconds: float | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = (
            timeout_seconds if timeout_seconds is not None else settings.AI_TIMEOUT_SECONDS
        )
        self._health_cache: LLMHealth | None = None
        self._health_cache_time: float = 0.0

    @property
    def is_configured(self) -> bool:
        """Le moteur est utilisable si l'IA est globalement active et la clé présente."""
        return bool(settings.AI_ENABLED and self.api_key.strip())

    # --- Traduction filaire, à fournir par chaque adaptateur ---

    @abstractmethod
    def chat_endpoint(self) -> str:
        """URL absolue de l'appel de génération de texte."""

    @abstractmethod
    def chat_headers(self) -> dict[str, str]:
        """En-têtes d'authentification et de contenu de l'appel de génération."""

    @abstractmethod
    def build_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        image_urls: list[str] | None,
    ) -> dict[str, Any]:
        """Traduit le contrat du projet vers le corps de requête du fournisseur."""

    @abstractmethod
    def extract_text(self, data: Any) -> str | None:
        """Isole le texte produit d'une réponse décodée, ou ``None``."""

    @abstractmethod
    def health_request(self) -> tuple[str, dict[str, str]]:
        """URL et en-têtes de la sonde de santé, spécifique du fournisseur."""

    # --- Comportement commun ---

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        image_urls: list[str] | None = None,
        timeout: float | None = None,
    ) -> str | None:
        """Un POST, une réponse, aucun lever : l'échec vaut `None`, jamais une exception."""
        if not self.is_configured:
            logger.debug("Appel LLM ignoré : clé API absente ou IA désactivée.")
            return None

        try:
            response = await get_shared_http_client().post(
                self.chat_endpoint(),
                json=self.build_payload(system_prompt, user_prompt, temperature, image_urls),
                headers=self.chat_headers(),
                timeout=self.timeout if timeout is None else timeout,
            )
            response.raise_for_status()
            return self.extract_text(response.json())
        except httpx.TimeoutException:
            logger.warning("Timeout lors de l'appel LLM (%ss) sur %s", self.timeout, self.model)
            return None
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Erreur HTTP %s du moteur LLM %s",
                exc.response.status_code,
                self.vendor,
            )
            return None
        except Exception as exc:
            logger.warning("Réponse ou appel LLM inexploitable (%s).", type(exc).__name__)
            return None

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> dict[str, object] | None:
        """Rend un objet JSON, ou ``None``. Ne lève jamais : l'IA ne bloque jamais le bot."""
        if not self.is_configured:
            logger.debug("Appel LLM ignoré : clé API absente ou IA désactivée.")
            return None
        raw_text = await self.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
        )
        return clean_and_parse_json(raw_text) if raw_text else None

    async def check_health(self, force: bool = False) -> LLMHealth:
        """Sonde le fournisseur, avec un cache de 30 secondes pour ménager le quota."""
        if not settings.AI_ENABLED:
            return LLMHealth(
                status=STATUS_DISABLED,
                model=self.model,
                message="IA désactivée (AI_ENABLED=False)",
            )
        if not self.api_key.strip():
            return LLMHealth(
                status=STATUS_UNCONFIGURED,
                model=self.model,
                message="Clé API non configurée",
            )

        now = time.monotonic()
        if (
            not force
            and self._health_cache
            and (now - self._health_cache_time) < HEALTH_CACHE_SECONDS
        ):
            return self._health_cache

        url, headers = self.health_request()
        start = time.monotonic()
        try:
            response = await get_shared_http_client().get(
                url, headers=headers, timeout=HEALTH_TIMEOUT_SECONDS
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if response.status_code == 200:
                health = LLMHealth(
                    status=STATUS_ACTIVE,
                    model=self.model,
                    latency_ms=elapsed_ms,
                    message=f"Sonde HTTP accessible ({elapsed_ms}ms)",
                )
            elif response.status_code in (401, 403):
                health = LLMHealth(
                    status=STATUS_ERROR,
                    model=self.model,
                    latency_ms=elapsed_ms,
                    message="Clé API invalide ou refusée",
                )
            else:
                health = LLMHealth(
                    status=STATUS_ERROR,
                    model=self.model,
                    latency_ms=elapsed_ms,
                    message=f"Erreur HTTP {response.status_code}",
                )
        except httpx.TimeoutException:
            health = LLMHealth(
                status=STATUS_ERROR,
                model=self.model,
                message="Délai d'attente dépassé (timeout)",
            )
        except Exception as exc:
            health = LLMHealth(
                status=STATUS_ERROR,
                model=self.model,
                message=f"Erreur réseau ({type(exc).__name__})",
            )

        self._health_cache = health
        self._health_cache_time = start
        return health


def clean_and_parse_json(raw_text: str) -> dict[str, object] | None:
    """Nettoie et parse une réponse JSON, même encadrée de balises Markdown.

    Indépendant du fournisseur : un modèle qui rend du JSON dans un bloc est la
    norme, pas l'exception.
    """
    if not raw_text:
        return None

    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("Réponse LLM non décodable en JSON (%s) : %s", exc, raw_text[:200])
        return None
    if not isinstance(parsed, dict):
        logger.warning("La réponse LLM n'est pas un objet JSON : %s", parsed)
        return None
    return parsed
