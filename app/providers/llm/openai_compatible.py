"""Adaptateur OpenAI-compatible : DeepSeek, Groq, Mistral, OpenRouter, Ollama, LM Studio.

Une seule écriture HTTP pour tous ces fournisseurs : `POST {base_url}/chat/completions`
avec un jeton Bearer, et une sonde `GET {base_url}/models`. Un fournisseur qui ne
parle pas ce dialecte a son propre adaptateur.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.providers.llm.base import BaseLLMClient

logger = logging.getLogger(__name__)

# Nombre maximal d'images envoyées : au-delà, le coût explose sans gain de décision.
MAX_IMAGES = 5


class OpenAICompatibleClient(BaseLLMClient):
    """Client de chat compatible OpenAI, en JSON strict."""

    vendor = "openai-compatible"

    # Le mode raisonnement est ACTIVÉ PAR DÉFAUT chez DeepSeek, avec un effort
    # « high », et il neutralise alors `temperature`. Mesuré sur les prompts de
    # production (`scripts/probe_llm_latency.py`) : le prompt variantes passe de
    # 12,3 s — au-delà du budget, donc perdu — à 1,4 s sans raisonnement ; le
    # parsing `/add` de 10,9 s à 1,2 s. Pour un projet qui tient une ligne de
    # latence à chaque appel, ce n'est pas un réglage mais une source de pannes.
    #
    # C'est un champ de dialecte, donc il vit ici, dans la seule frontière qui
    # connaît un fournisseur (invariant n°4). Un endpoint qui refuserait un champ
    # inconnu n'a qu'à passer cet attribut à `None` : le comportement par défaut du
    # fournisseur est alors rendu tel quel.
    reasoning_disabled: dict[str, Any] | None = {"type": "disabled"}

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(
            model=model or settings.AI_MODEL,
            api_key=(
                api_key if api_key is not None else settings.AI_API_KEY.get_secret_value()
            ),
            base_url=base_url or settings.AI_BASE_URL,
            timeout_seconds=timeout_seconds,
        )

    def chat_endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def chat_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def build_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        image_urls: list[str] | None,
    ) -> dict[str, Any]:
        """`response_format` impose le JSON, l'image passe en bloc `image_url`."""
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if image_urls:
            content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
            for url in image_urls[:MAX_IMAGES]:
                if url.startswith("http"):
                    content.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user_prompt})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": temperature,
        }
        if self.reasoning_disabled is not None:
            payload["thinking"] = self.reasoning_disabled
        return payload

    def extract_text(self, data: Any) -> str | None:
        """Isole `choices[0].message.content`."""
        if not isinstance(data, dict):
            return None
        choices = data.get("choices") or []
        if not choices:
            logger.warning("Réponse LLM vide ou sans 'choices'.")
            return None
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        return content.strip() or None if isinstance(content, str) else None

    def health_request(self) -> tuple[str, dict[str, str]]:
        return f"{self.base_url}/models", {"Authorization": f"Bearer {self.api_key}"}
