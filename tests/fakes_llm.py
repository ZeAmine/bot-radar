"""Faux moteur IA partagé par les tests.

Il implémente le contrat `LLMClient` sans dépendre d'un adaptateur réel : c'est ce
qui permet d'écrire un test de bout en bout du `/add` sans nommer le modèle, et de
le laisser tel quel lors d'un changement de fournisseur.
"""

from __future__ import annotations

import json
from typing import Any

from app.providers.llm.base import clean_and_parse_json


class FakeLLM:
    """Moteur simulé : renvoie un texte, journalise les appels, n'ouvre aucune connexion."""

    def __init__(
        self,
        text: str | dict[str, Any] | None = None,
        *,
        api_key: str = "fake-key",
        enabled: bool = True,
        vendor: str = "fake",
        model: str = "fake-model",
        error: Exception | None = None,
    ) -> None:
        self.api_key = api_key
        self.enabled = enabled
        self.vendor = vendor
        self.model = model
        self.error = error
        if isinstance(text, (dict, list)):
            text = json.dumps(text, ensure_ascii=False)
        self.text = text
        self.calls: list[dict[str, Any]] = []

    @property
    def is_configured(self) -> bool:
        return bool(self.enabled and self.api_key.strip())

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        image_urls: list[str] | None = None,
        timeout: float | None = None,
    ) -> str | None:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
                "image_urls": image_urls,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        if not self.is_configured:
            return None
        return self.text

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> dict[str, object] | None:
        raw = await self.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
        )
        return clean_and_parse_json(raw) if raw else None

    async def check_health(self, force: bool = False) -> Any:
        from app.providers.llm.base import LLMHealth

        return LLMHealth(
            status="active", model=self.model, latency_ms=1, message=f"simulé (force={force})"
        )


class FlakyLLM:
    """Moteur qui répond autre chose à chaque appel, `None` compris.

    Le vrai client renvoie `None` après un délai dépassé, une erreur HTTP ou un JSON
    illisible : rien ne distingue alors une panne d'une réponse. Les réponses sont
    donc une suite, la dernière étant répétée si l'appelant insiste plus que prévu —
    ce qui rend visible un essai en trop.

    `configured=False` décrit l'autre extrême, celui d'un moteur absent : appelant
    encore, mais rien à qui demander.
    """

    def __init__(self, *responses: object, configured: bool = True) -> None:
        self.responses = list(responses) or [None]
        self.configured = configured
        self.calls = 0

    @property
    def is_configured(self) -> bool:
        return self.configured

    async def generate_json(self, **_kwargs: object) -> object:
        self.calls += 1
        return self.responses[min(self.calls, len(self.responses)) - 1]
