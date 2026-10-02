"""Adaptateur Anthropic (API Messages).

Cet adaptateur existe pour une raison précise : l'API Messages ne partage **rien**
avec le dialecte OpenAI. Pas de `/chat/completions`, pas de `response_format`, pas de
jeton Bearer. Le système est un champ de premier niveau, `max_tokens` est
obligatoire, et le JSON n'est obtenu que par instruction du prompt puis nettoyage.

C'est le test honnête de la frontière : si ce fichier passe le contrat de test sans
qu'une ligne au-dessus change, la frontière est au bon endroit.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.providers.llm.base import BaseLLMClient

logger = logging.getLogger(__name__)

API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 2048
MAX_IMAGES = 5
# Rappel de format : l'API Messages n'a pas de mode JSON, la contrainte vient du prompt.
JSON_INSTRUCTION = "Réponds uniquement par un objet JSON valide, sans texte autour."


class AnthropicClient(BaseLLMClient):
    """Client de chat Anthropic, en JSON obtenu par instruction du prompt."""

    vendor = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        super().__init__(
            model=model or settings.AI_MODEL,
            api_key=(
                api_key if api_key is not None else settings.AI_API_KEY.get_secret_value()
            ),
            base_url=base_url or settings.AI_BASE_URL,
            timeout_seconds=timeout_seconds,
        )
        self.max_tokens = max_tokens

    def chat_endpoint(self) -> str:
        return f"{self.base_url}/v1/messages"

    def chat_headers(self) -> dict[str, str]:
        """Authentification par en-tête dédié et version d'API figée par Anthropic."""
        return {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
        }

    def build_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        image_urls: list[str] | None,
    ) -> dict[str, Any]:
        """Le système sort du tableau `messages`, l'image devient une source explicite."""
        if image_urls:
            content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
            for url in image_urls[:MAX_IMAGES]:
                if url.startswith("http"):
                    content.append({"type": "image", "source": {"type": "url", "url": url}})
        else:
            content = [{"type": "text", "text": user_prompt}]
        return {
            "model": self.model,
            "system": f"{system_prompt}\n\n{JSON_INSTRUCTION}",
            "messages": [{"role": "user", "content": content}],
            "max_tokens": self.max_tokens,
            "temperature": temperature,
        }

    def extract_text(self, data: Any) -> str | None:
        """Isole la concaténation des blocs de texte de la réponse."""
        if not isinstance(data, dict):
            return None
        blocks = data.get("content") or []
        if not blocks:
            logger.warning("Réponse LLM vide ou sans contenu : %s", data)
            return None
        parts = [
            block["text"]
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ]
        if not parts:
            return None
        return "\n".join(parts).strip() or None

    def health_request(self) -> tuple[str, dict[str, str]]:
        return f"{self.base_url}/v1/models", {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
        }
