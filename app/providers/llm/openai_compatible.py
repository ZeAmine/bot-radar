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

    # DeepSeek active le raisonnement (« high ») par défaut : il neutralise
    # `temperature` et creuse la latence. Champ de dialecte, `None` si refusé.
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
        choices = data.get("choices") if isinstance(data, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else None
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(choice, dict) or not isinstance(message, dict):
            logger.warning(
                "Enveloppe LLM inexploitable.",
                extra={"event": "llm_response", "outcome": "invalid_envelope"},
            )
            return None
        if choice.get("finish_reason") in (
            "length", "content_filter", "insufficient_system_resource", "aborted",
        ):
            logger.warning(
                "Génération LLM interrompue.",
                extra={"event": "llm_response", "outcome": "interrupted"},
            )
            return None
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            logger.warning(
                "Enveloppe LLM inexploitable.",
                extra={"event": "llm_response", "outcome": "invalid_envelope"},
            )
            return None
        text = content.strip() if isinstance(content, str) else ""
        if not text:
            logger.warning(
                "Réponse LLM sans contenu.",
                extra={"event": "llm_response", "outcome": "empty_content"},
            )
        return text or None

    def health_request(self) -> tuple[str, dict[str, str]]:
        return f"{self.base_url}/models", {"Authorization": f"Bearer {self.api_key}"}
