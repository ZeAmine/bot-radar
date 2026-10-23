import re
from dataclasses import dataclass
from typing import Literal

from app.domain.rules.book_volume import (
    is_collection,
    is_volume_composition,
    requested_volume,
    volume_base_title,
)
from app.utils.text import normalize_text


@dataclass(frozen=True)
class AssociationDecision:
    outcome: Literal["accept", "reject", "abstain"]
    reason: str


def author_tokens(author: str | None) -> frozenset[str]:
    normalized = re.sub(r"\b(de|du|des|le|la|van|von)\s+", r"\1", normalize_text(author))
    return frozenset(normalized.split())


def series_association(
    *,
    requested_title: str,
    title: str,
    author: str | None,
    author_hint: str | None,
    language: str | None,
    number: int | None,
    exact_isbn: bool = False,
) -> AssociationDecision:
    if language and language.lower() not in {"fr", "fra", "fre", "und"}:
        return AssociationDecision("reject", "foreign_language")
    if not language or language.lower() == "und":
        return AssociationDecision("abstain", "language_unproven")
    wanted = requested_volume(requested_title)
    if wanted is not None:
        if (
            is_collection(title)
            or is_volume_composition(title)
        ):
            return AssociationDecision("reject", "collection_for_volume")
        if number is not None and wanted != number:
            return AssociationDecision("reject", "volume_conflict")
        if number is None:
            return AssociationDecision("abstain", "volume_unproven")
    if exact_isbn:
        return AssociationDecision("accept", "exact_isbn_evidence")
    expected_title = volume_base_title(requested_title)
    actual_title = volume_base_title(title)
    if not expected_title or actual_title != expected_title:
        return AssociationDecision("abstain", "work_title_unproven")
    if author_hint:
        expected = author_tokens(author_hint)
        actual = author_tokens(author)
        if not actual:
            return AssociationDecision("abstain", "author_unproven")
        if actual < expected:
            return AssociationDecision("abstain", "author_unproven")
        if not expected <= actual:
            return AssociationDecision("reject", "author_conflict")
    else:
        return AssociationDecision("abstain", "work_unproven")
    return AssociationDecision("accept", "series_evidence")
