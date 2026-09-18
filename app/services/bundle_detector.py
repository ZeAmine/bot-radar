"""Détection automatique et analyse sémantique des annonces de type 'Lots' (Bundle Hunter)."""

import re
from typing import NamedTuple


class BundleDetectionResult(NamedTuple):
    is_bundle: bool
    estimated_item_count: int | None
    matched_pattern: str | None


BUNDLE_PATTERNS = [
    r"\blot\s+(?:de\s+)?(?P<count>\d+)\s*(?:bd|bandes\s*dessin[eé]es|mangas?|livres?|tomes?|comics?|albums?|jeux|consoles|articles|pieces|pièces)\b",
    r"\b(?P<count>\d+)\s*(?:bd|bandes\s*dessin[eé]es|mangas?|livres?|tomes?|comics?|albums?|jeux|consoles|articles|pieces|pièces)\b",
    r"\blot\s+de\s+(?P<count>\d+)\b",
    r"\bint[eé]grale\s+(?:de\s+)?(?P<count>\d+)\s+tomes?\b",
    r"\blot\s+bd\b",
    r"\blot\s+manga\b",
    r"\blot\s+livres?\b",
    r"\blot\s+comics?\b",
    r"\blot\b",
    r"\bpack\b",
    r"\bensemble\b",
    r"\bbundle\b",
    r"\bcollection\b",
]


class BundleDetector:
    """Détecte les annonces composées de plusieurs articles (lots / packs)."""

    @classmethod
    def detect(cls, title: str, description: str = "") -> BundleDetectionResult:
        """Analyse le titre et la description d'une annonce pour identifier un lot."""
        text = f"{title} {description}".lower()

        for pattern in BUNDLE_PATTERNS:
            match = re.search(pattern, text)
            if match:
                item_count = None
                if "count" in match.groupdict() and match.group("count"):
                    try:
                        c = int(match.group("count"))
                        if 1 < c <= 100:
                            item_count = c
                    except ValueError:
                        pass

                return BundleDetectionResult(
                    is_bundle=True,
                    estimated_item_count=item_count,
                    matched_pattern=match.group(0),
                )

        return BundleDetectionResult(
            is_bundle=False,
            estimated_item_count=None,
            matched_pattern=None,
        )
