from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.core.config import settings
from app.providers.llm.openai_compatible import OpenAICompatibleClient


def strict_output_schema(response_model: type[BaseModel]) -> dict[str, Any]:
    schema = response_model.model_json_schema()

    def normalize(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                normalize(value)
        elif isinstance(node, list):
            for item in node:
                normalize(item)

    normalize(schema)
    return schema


class OpenAIClient(OpenAICompatibleClient):
    vendor = "openai"
    completion_budget_field = "max_completion_tokens"

    def build_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        image_urls: list[str] | None,
        *,
        response_model: type[BaseModel] | None = None,
    ) -> dict[str, Any]:
        payload = super().build_payload(system_prompt, user_prompt, temperature, image_urls)
        payload[self.completion_budget_field] = payload.pop("max_tokens")
        payload["reasoning_effort"] = settings.AI_REASONING_EFFORT
        if response_model is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": strict_output_schema(response_model),
                },
            }
        return payload
