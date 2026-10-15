"""Fournisseurs de moteur IA.

Ce package est la seule frontière du projet qui connaît un fournisseur. Le reste de
l'application parle au client construit par `registry.py` et n'a pas à changer quand
le modèle change.
"""

from app.providers.llm.base import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_UNCONFIGURED,
    BaseLLMClient,
    LLMClient,
    LLMHealth,
    clean_and_parse_json,
)
from app.providers.llm.registry import LLM_ADAPTERS, build_llm_client, llm_client

__all__ = [
    "STATUS_ACTIVE",
    "STATUS_DISABLED",
    "STATUS_ERROR",
    "STATUS_UNCONFIGURED",
    "LLM_ADAPTERS",
    "BaseLLMClient",
    "LLMClient",
    "LLMHealth",
    "build_llm_client",
    "clean_and_parse_json",
    "llm_client",
]
