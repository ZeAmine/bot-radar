import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.providers.llm.base import LLMHealth
from app.providers.llm.openai import OpenAIClient


class RiverClient(OpenAIClient):
    vendor = "river"
    completion_budget_field = "max_tokens"
    _last_call: dict[str, object] | None = None

    def _safe_identifier(self, value: object) -> str | int | None:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
            return None
        if self.api_key and self.api_key in value:
            return None
        if re.search(r"rv_|sk-|gh[pousr]_|github_pat_", value, re.IGNORECASE):
            return None
        return value

    def response_diagnostics(self, response: httpx.Response) -> dict[str, object]:
        result: dict[str, object] = {
            "x_request_id": self._safe_identifier(response.headers.get("x-request-id")),
            "provider_code": None,
            "provider_type": None,
        }
        if response.is_error:
            try:
                body = response.json()
            except ValueError:
                return result
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                result.update(provider_code=self._safe_identifier(error.get("code")),
                              provider_type=self._safe_identifier(error.get("type")))
        return result

    def record_completion(self, diagnostics: dict[str, Any]) -> None:
        self._last_call = {
            "observed_at": datetime.now(UTC).isoformat(),
            "status": diagnostics["status"],
            "error_category": None if diagnostics["status"] == "ok" else diagnostics["status"],
            **{key: diagnostics.get(key) for key in (
                "http_status", "provider_code", "provider_type", "x_request_id", "duration_ms",
            )},
        }

    async def check_health(self, force: bool = False) -> LLMHealth:
        del force
        configured = bool(self.api_key.strip())
        values: dict[str, Any] = {
            "model": self.model, "configured": configured, "last_call": self._last_call,
        }
        if not settings.AI_ENABLED:
            return LLMHealth(status="disabled", message="IA désactivée.", **values)
        if not configured:
            return LLMHealth(status="unconfigured", message="Clé API non configurée.", **values)
        if self._last_call is None:
            return LLMHealth(
                status="unverified", message="IA configurée, aucun appel réel observé.", **values,
            )
        if self._last_call["status"] == "ok":
            return LLMHealth(status="last_call_ok", message="Dernier appel réel réussi.", **values)
        return LLMHealth(status="last_call_error", message="Dernier appel réel échoué.", **values)
