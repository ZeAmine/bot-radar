"""Tests du point de bascule du moteur IA.

Une faute de frappe dans `AI_PROVIDER` doit échouer au démarrage, pas éteindre l'IA
en silence : c'est tout l'objet de ce registre.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.providers.llm.anthropic import AnthropicClient
from app.providers.llm.openai_compatible import OpenAICompatibleClient
from app.providers.llm.registry import LLM_ADAPTERS, build_llm_client, llm_client


def test_shipped_adapters_are_registered() -> None:
    """Les deux adaptateurs livrés sont dans le registre, avec leur classe."""
    assert {
        "openai-compatible": OpenAICompatibleClient,
        "anthropic": AnthropicClient,
    } == LLM_ADAPTERS


def test_build_client_uses_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`AI_PROVIDER` choisit l'adaptateur, sans nommer de fournisseur ailleurs."""
    monkeypatch.setattr(settings, "AI_API_KEY", SecretStr("k"))
    monkeypatch.setattr(settings, "AI_MODEL", "un-modele")
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://exemple.test/v1")

    monkeypatch.setattr(settings, "AI_PROVIDER", "anthropic")
    anthropic = build_llm_client()
    assert isinstance(anthropic, AnthropicClient)
    assert anthropic.model == "un-modele"
    assert anthropic.chat_endpoint() == "https://exemple.test/v1/v1/messages"

    monkeypatch.setattr(settings, "AI_PROVIDER", "openai-compatible")
    assert isinstance(build_llm_client(), OpenAICompatibleClient)


def test_build_client_is_case_and_space_insensitive() -> None:
    """Un nom de fournisseur mal saisi en casse ne casse pas le démarrage."""
    assert isinstance(build_llm_client("  Anthropic "), AnthropicClient)


def test_unknown_provider_fails_loudly() -> None:
    """Un fournisseur inconnu lève, avec la liste de ceux qui existent."""
    with pytest.raises(ValueError, match="Fournisseur LLM inconnu") as exc_info:
        build_llm_client("deepseek-v9")

    message = str(exc_info.value)
    assert "deepseek-v9" in message
    assert "anthropic" in message
    assert "openai-compatible" in message


def test_explicit_provider_overrides_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """L'argument prime sur la configuration : le registre est testable sans environment global."""
    monkeypatch.setattr(settings, "AI_PROVIDER", "inconnu")
    assert isinstance(build_llm_client("anthropic"), AnthropicClient)


def test_module_singleton_resolves_to_a_registered_adapter() -> None:
    """Le singleton du projet est bien l'un des adaptateurs du registre."""
    assert isinstance(llm_client, (OpenAICompatibleClient, AnthropicClient))
    assert type(llm_client) in LLM_ADAPTERS.values()
