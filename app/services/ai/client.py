"""Client HTTP optimisé pour l'API DeepSeek-V4.1-Flash avec support texte et vision native."""

import json
import logging
import re
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.http_client import get_shared_http_client

logger = logging.getLogger(__name__)


class DeepSeekClient:
    """Client asynchrone pour interroger DeepSeek-V4.1-Flash."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 12.0,
    ) -> None:
        self.api_key = (
            api_key if api_key is not None else settings.DEEPSEEK_API_KEY.get_secret_value()
        )
        self.base_url = (base_url or settings.DEEPSEEK_BASE_URL).rstrip("/")
        self.model = model or settings.DEEPSEEK_MODEL
        self.timeout = timeout_seconds
        self._health_cache: dict[str, Any] | None = None
        self._health_cache_time: float = 0.0

    @property
    def is_configured(self) -> bool:
        """Vérifie si le client dispose d'une clé API et est activé."""
        return bool(settings.AI_ENABLED and self.api_key and self.api_key.strip())

    async def check_health(self, force: bool = False) -> dict[str, Any]:
        """Vérifie l'état opérationnel du moteur DeepSeek avec mise en cache de 30 secondes.

        Retourne un dictionnaire contenant :
        - status : 'active' | 'unconfigured' | 'disabled' | 'error'
        - model : nom du modèle configuré
        - latency_ms : temps de réponse en millisecondes (si actif ou erreur)
        - message : message descriptif
        """
        if not settings.AI_ENABLED:
            return {
                "status": "disabled",
                "model": self.model,
                "latency_ms": None,
                "message": "IA désactivée (AI_ENABLED=False)",
            }

        if not self.api_key or not self.api_key.strip():
            return {
                "status": "unconfigured",
                "model": self.model,
                "latency_ms": None,
                "message": "Clé API non configurée",
            }

        now = time.monotonic()
        if not force and self._health_cache and (now - self._health_cache_time) < 30.0:
            return self._health_cache

        endpoint = f"{self.base_url}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        start_time = time.monotonic()
        try:
            client = get_shared_http_client()
            resp = await client.get(endpoint, headers=headers, timeout=4.0)
            elapsed_ms = int((time.monotonic() - start_time) * 1000)

            if resp.status_code == 200:
                result = {
                    "status": "active",
                    "model": self.model,
                    "latency_ms": elapsed_ms,
                    "message": f"Opérationnel ({elapsed_ms}ms)",
                }
            elif resp.status_code in (401, 403):
                result = {
                    "status": "error",
                    "model": self.model,
                    "latency_ms": elapsed_ms,
                    "message": "Clé API invalide ou refusée",
                }
            else:
                result = {
                    "status": "error",
                    "model": self.model,
                    "latency_ms": elapsed_ms,
                    "message": f"Erreur HTTP {resp.status_code}",
                }
        except httpx.TimeoutException:
            result = {
                "status": "error",
                "model": self.model,
                "latency_ms": None,
                "message": "Délai d'attente dépassé (timeout)",
            }
        except Exception as exc:
            result = {
                "status": "error",
                "model": self.model,
                "latency_ms": None,
                "message": f"Erreur réseau ({type(exc).__name__})",
            }

        self._health_cache = result
        self._health_cache_time = now
        return result

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> dict[str, Any] | None:
        """Envoie une requête et retourne la réponse sous forme de dictionnaire JSON."""
        if not self.is_configured:
            logger.debug("Appel DeepSeek ignoré : clé API non configurée ou IA désactivée.")
            return None

        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        user_message: dict[str, Any] = {"role": "user", "content": user_prompt}

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                user_message,
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
        }

        try:
            client = get_shared_http_client()
            response = await client.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()

            choices = data.get("choices", [])
            if not choices:
                logger.warning("Réponse DeepSeek vide ou sans 'choices': %s", data)
                return None

            raw_text = choices[0].get("message", {}).get("content", "").strip()
            return self._clean_and_parse_json(raw_text)

        except httpx.TimeoutException:
            logger.warning("Timeout lors de l'appel à DeepSeek (%ss)", self.timeout)
            return None
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Erreur HTTP DeepSeek (%s): %s",
                exc.response.status_code,
                exc.response.text,
            )
            return None
        except Exception as exc:
            logger.exception("Erreur inattendue lors de l'appel DeepSeek: %s", exc)
            return None

    @staticmethod
    def _clean_and_parse_json(raw_text: str) -> dict[str, Any] | None:
        """Nettoie et parse la réponse JSON, même si encadrée par des balises markdown."""
        if not raw_text:
            return None

        cleaned = raw_text.strip()
        # Supprimer les balises de code Markdown éventuelles ```json ... ```
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            cleaned = cleaned.strip()

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
            logger.warning("Le JSON retourné par DeepSeek n'est pas un dictionnaire : %s", parsed)
            return None
        except json.JSONDecodeError as exc:
            logger.warning("Échec du parsing JSON DeepSeek (%s): %s", exc, raw_text[:200])
            return None


# Instance singleton globale
deepseek_client = DeepSeekClient()
