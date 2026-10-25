"""Adaptateur OpenAI-compatible : DeepSeek, Groq, Mistral, OpenRouter, Ollama, LM Studio.

Une seule écriture HTTP pour tous ces fournisseurs : `POST {base_url}/chat/completions`
avec un jeton Bearer, et une sonde `GET {base_url}/models`. Un fournisseur qui ne
parle pas ce dialecte a son propre adaptateur.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from app.core.config import settings
from app.providers.llm.base import BaseLLMClient, LLMResponseError, usage_count

# Nombre maximal d'images envoyées : au-delà, le coût explose sans gain de décision.
MAX_IMAGES = 5


class OpenAICompatibleClient(BaseLLMClient):
    """Client de chat compatible OpenAI, en JSON strict."""

    vendor = "openai-compatible"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        super().__init__(
            model=model or settings.AI_MODEL,
            api_key=(
                api_key if api_key is not None else settings.AI_API_KEY.get_secret_value()
            ),
            base_url=base_url or settings.AI_BASE_URL,
            timeout_seconds=timeout_seconds,
        )

        self.max_tokens = max_tokens if max_tokens is not None else settings.AI_MAX_TOKENS

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
        *,
        response_model: type[BaseModel] | None = None,
    ) -> dict[str, Any]:
        """`response_format` impose le JSON, l'image passe en bloc `image_url`."""
        if response_model is not None:
            system_prompt += "\n\nSchéma JSON attendu : " + json.dumps(
                response_model.model_json_schema(), ensure_ascii=False,
            )
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
            "max_tokens": self.max_tokens,
        }
        return payload

    def extract_text(self, data: Any) -> str | None:
        """Isole `choices[0].message.content`."""
        choices = data.get("choices") if isinstance(data, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else None
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(choice, dict) or not isinstance(message, dict):
            raise LLMResponseError("invalid_envelope")
        if message.get("refusal"):
            raise LLMResponseError("refused")
        if choice.get("finish_reason") in (
            "length", "content_filter", "insufficient_system_resource", "aborted",
        ):
            raise LLMResponseError("interrupted")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise LLMResponseError("invalid_envelope")
        text = content.strip() if isinstance(content, str) else ""
        if not text:
            raise LLMResponseError("empty_content")
        return text

    def extract_usage(self, data: Any) -> dict[str, int | None]:
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        prompt = usage.get("prompt_tokens_details")
        completion = usage.get("completion_tokens_details")
        return {
            "input": usage_count(usage.get("prompt_tokens")),
            "output": usage_count(usage.get("completion_tokens")),
            "cached": usage_count(prompt.get("cached_tokens"))
            if isinstance(prompt, dict) else None,
            "reasoning": usage_count(completion.get("reasoning_tokens"))
            if isinstance(completion, dict) else None,
        }

    def health_request(self) -> tuple[str, dict[str, str]]:
        return f"{self.base_url}/models", {"Authorization": f"Bearer {self.api_key}"}
