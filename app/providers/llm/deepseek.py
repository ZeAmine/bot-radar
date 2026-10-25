from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.providers.llm.openai_compatible import OpenAICompatibleClient


class DeepSeekClient(OpenAICompatibleClient):
    vendor = "deepseek"

    def build_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        image_urls: list[str] | None,
        *,
        response_model: type[BaseModel] | None = None,
    ) -> dict[str, Any]:
        payload = super().build_payload(
            system_prompt, user_prompt, temperature, image_urls, response_model=response_model,
        )
        payload["thinking"] = {"type": "disabled"}
        return payload
