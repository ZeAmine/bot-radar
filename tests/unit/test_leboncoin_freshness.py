"""Tests de l'admission temporelle des annonces Leboncoin."""

from datetime import UTC, datetime, timedelta

from app.domain.rules.listing_freshness import (
    listing_is_new_for_alert,
    listing_is_new_for_leboncoin,
)


class FakeAlert:
    def __init__(self, created_at: datetime, baseline: dict | None = None) -> None:
        self.created_at = created_at
        self.listing_baseline = baseline


class FakeListing:
    def __init__(
        self,
        source_name: str,
        *,
        external_id: str = "123",
        received_at: datetime | None = None,
        published_at: datetime | None = None,
    ) -> None:
        self.source_name = source_name
        self.external_id = external_id
        self.received_at = received_at or datetime.now(UTC)
        self.published_at = published_at


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def test_leboncoin_listing_published_after_alert_creation_is_new():
    alert = FakeAlert(created_at=NOW)
    listing = FakeListing("leboncoin", published_at=NOW + timedelta(minutes=5))
    assert listing_is_new_for_leboncoin(alert, listing) is True
    assert listing_is_new_for_alert(alert, listing) is True


def test_leboncoin_listing_published_before_alert_creation_is_not_new():
    alert = FakeAlert(created_at=NOW)
    listing = FakeListing("leboncoin", published_at=NOW - timedelta(minutes=5))
    assert listing_is_new_for_leboncoin(alert, listing) is False
    assert listing_is_new_for_alert(alert, listing) is False


def test_leboncoin_listing_without_publication_date_is_not_new():
    alert = FakeAlert(created_at=NOW)
    listing = FakeListing("leboncoin", published_at=None)
    assert listing_is_new_for_leboncoin(alert, listing) is False
    assert listing_is_new_for_alert(alert, listing) is False


def test_leboncoin_ignores_a_pending_vinted_baseline():
    """La source Leboncoin ne dépend pas de l'empreinte de premier scan Vinted."""
    alert = FakeAlert(created_at=NOW, baseline={"state": "pending"})
    listing = FakeListing("leboncoin", published_at=NOW + timedelta(minutes=1))
    assert listing_is_new_for_alert(alert, listing) is True


def test_leboncoin_naive_datetime_is_treated_as_utc():
    alert = FakeAlert(created_at=datetime(2026, 10, 19, 12, 0))
    listing = FakeListing("leboncoin", published_at=datetime(2026, 10, 19, 12, 30))
    assert listing_is_new_for_alert(alert, listing) is True


def test_vinted_baseline_still_gates_vinted_listings():
    alert = FakeAlert(
        created_at=NOW,
        baseline={
            "state": "ready",
            "started_at": NOW.isoformat(),
            "external_ids": ["123"],
        },
    )
    listing = FakeListing("vinted", external_id="123", published_at=None)
    assert listing_is_new_for_alert(alert, listing) is False
