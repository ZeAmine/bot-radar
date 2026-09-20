"""Contrats d'interface (Protocols) du domaine."""

from app.domain.protocols.llm import LLMAnalysisProviderProtocol
from app.domain.protocols.matching import ListingMatcherProtocol
from app.domain.protocols.repositories import (
    AlertRepositoryProtocol,
    ListingRepositoryProtocol,
)

__all__ = [
    "AlertRepositoryProtocol",
    "LLMAnalysisProviderProtocol",
    "ListingMatcherProtocol",
    "ListingRepositoryProtocol",
]
