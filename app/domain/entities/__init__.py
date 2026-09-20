"""Entités pures du domaine métier."""

from app.domain.entities.alert import AlertEntity
from app.domain.entities.analysis import ListingAnalysisResult, RealConditionAssessment
from app.domain.entities.listing import ListingEntity
from app.domain.entities.matching import MatchResult

__all__ = [
    "AlertEntity",
    "ListingAnalysisResult",
    "ListingEntity",
    "MatchResult",
    "RealConditionAssessment",
]

