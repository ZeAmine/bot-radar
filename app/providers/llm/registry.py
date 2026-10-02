"""Point de bascule du moteur IA.

`AI_PROVIDER` dans l'environnement choisit l'adaptateur. Changer de modèle entre
dans le dictionnaire ci-dessous, ou n'y touche pas quand le nouveau fournisseur
parle le dialecte OpenAI : dans ce cas, seules `AI_BASE_URL`, `AI_MODEL` et
`AI_API_KEY` changent.

Un fournisseur inconnu lève une erreur au démarrage : une faute de frappe dans
`AI_PROVIDER` doit se voir tout de suite, pas éteindre l'IA en silence.
"""

from __future__ import annotations

from app.core.config import settings
from app.providers.llm.anthropic import AnthropicClient
from app.providers.llm.base import LLMClient
from app.providers.llm.openai_compatible import OpenAICompatibleClient

# Clé = valeur de `AI_PROVIDER`.
LLM_ADAPTERS: dict[str, type[OpenAICompatibleClient | AnthropicClient]] = {
    "openai-compatible": OpenAICompatibleClient,
    "anthropic": AnthropicClient,
}


def build_llm_client(provider: str | None = None) -> LLMClient:
    """Construit le client du fournisseur demandé, d'après la configuration."""
    name = (provider or settings.AI_PROVIDER).strip().lower()
    adapter = LLM_ADAPTERS.get(name)
    if adapter is None:
        raise ValueError(
            f"Fournisseur LLM inconnu : {name!r}. "
            f"Disponibles : {', '.join(sorted(LLM_ADAPTERS))}."
        )
    return adapter()


llm_client: LLMClient = build_llm_client()
