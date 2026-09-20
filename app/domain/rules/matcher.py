"""Implémentation concrète du moteur de matching déterministe."""

from app.domain.entities.alert import AlertEntity
from app.domain.entities.listing import ListingEntity
from app.domain.entities.matching import MatchResult
from app.domain.enums import MatchConfidence
from app.domain.protocols.matching import ListingMatcherProtocol
from app.services.matcher_service import evaluate_listing_against_alert


class DeterministicListingMatcher(ListingMatcherProtocol):
    """Moteur de matching déterministe en mémoire (CPU pur, zéro I/O, zéro DB)."""

    def evaluate(
        self,
        listing: ListingEntity,
        alert: AlertEntity,
    ) -> MatchResult:
        """Évalue une annonce contre une alerte via les règles métier strictes."""
        legacy_result = evaluate_listing_against_alert(
            alert=alert,  # type: ignore[arg-type]
            listing_category_slug=listing.category_slug.value,
            listing_normalized_title=listing.normalized_title,
            listing_normalized_description=listing.description,
            listing_condition=listing.condition,
            listing_item_price_cents=listing.item_price_cents,
            listing_isbn13=listing.isbn13,
            listing_raw_url=listing.url,
        )

        conf = (
            MatchConfidence.HIGH
            if legacy_result.keyword_match_score > 0
            else MatchConfidence.MEDIUM
        )
        return MatchResult(
            is_match=legacy_result.is_match,
            keyword_match_score=legacy_result.keyword_match_score,
            condition_matches=legacy_result.condition_matches,
            price_matches=legacy_result.price_matches,
            confidence=conf,
            reason=legacy_result.reason,
            is_bundle=legacy_result.is_bundle,
        )

