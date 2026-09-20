"""Entité représentant le résultat d'une évaluation de matching."""

from dataclasses import dataclass, field

from app.domain.enums import MatchConfidence


@dataclass(slots=True)
class MatchResult:
    """Résultat d'évaluation d'une annonce par rapport à une alerte."""

    is_match: bool
    keyword_match_score: int = 0
    condition_matches: bool = True
    price_matches: bool = True
    confidence: MatchConfidence = MatchConfidence.HIGH
    reason: str = ""
    is_bundle: bool = False
    matched_keywords: list[str] = field(default_factory=list)
