"""Contrat de fraîcheur : deux composantes, deux échéances, deux rythmes.

Le modèle remplace un rythme uniforme de sept jours et une échelle de relance à
six heures, un jour, sept jours. Ce qui est verrouillé ici, c'est le contrat écrit
dans `app/services/market_refresh_policy.py` : la forme de `refresh_state`, le
rythme de chaque zone, la lecture immédiate de l'historique après une baisse de
plus de 30 %, et la péremption qui décide si une cote s'affiche encore comme un
montant.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.book_market_cache import BookMarketCache
from app.services import market_refresh_policy as policy
from app.services.market_refresh_policy import (
    COLD_CADENCE,
    DISPLAY_TOLERANCE,
    HISTORY_CADENCE,
    HOT_CADENCE,
    WARM_CADENCE,
    collection_due,
    component_due,
    cote_age,
    cote_cadence,
    cote_is_stale,
    cote_zone,
    due_components,
    fresh_cote_cents,
    out_of_rhythm,
    record_component,
    zone_rank,
)

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)


def _cache(**fields) -> BookMarketCache:
    cache = BookMarketCache(book_id=uuid.uuid4())
    for key, value in fields.items():
        setattr(cache, key, value)
    return cache


# --------------------------------------------------------------------------- #
# Le contrat de refresh_state est écrit et lisible
# --------------------------------------------------------------------------- #


def test_the_refresh_state_contract_is_documented_in_the_module():
    """Le contrat vit dans le module : un futur lecteur n'a pas à le deviner."""
    documentation = policy.__doc__ or ""
    for element in ("due_at", "last_success_at", "failures", "zone",
                    "cadence_seconds", "price_cents", "administrative"):
        assert element in documentation
    assert "book_market_cache" in documentation.lower() or "BookMarketCache" in documentation


def test_a_success_writes_the_documented_shape():
    cache = _cache()

    record_component(cache, "current", succeeded=True, now=NOW,
                     price_cents=2000, ceiling_cents=2000)
    record_component(cache, "history", succeeded=True, now=NOW)

    current = cache.refresh_state["current"]
    assert current["due_at"] == (NOW + HOT_CADENCE).isoformat()
    assert current["last_success_at"] == NOW.isoformat()
    assert current["failures"] == 0
    assert current["zone"] == "hot"
    assert current["cadence_seconds"] == HOT_CADENCE.total_seconds()
    assert current["price_cents"] == 2000
    history = cache.refresh_state["history"]
    assert history["due_at"] == (NOW + HISTORY_CADENCE).isoformat()
    assert history["cadence_seconds"] == HISTORY_CADENCE.total_seconds()


def test_the_next_refresh_column_follows_the_soonest_component():
    """`next_refresh_at` reste la prochaine échéance, jamais un créneau arbitraire."""
    cache = _cache()

    record_component(cache, "current", succeeded=True, now=NOW,
                     price_cents=2000, ceiling_cents=2000)
    record_component(cache, "history", succeeded=True, now=NOW)

    assert cache.next_refresh_at == NOW + HOT_CADENCE


# --------------------------------------------------------------------------- #
# Les zones et leurs rythmes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("price", "ceiling", "zone"), [
    (1000, 1000, "hot"),
    (1200, 1000, "hot"),      # exactement 1,2 × le plafond : encore chaud
    (1201, 1000, "warm"),
    (2000, 1000, "warm"),     # exactement 2 × le plafond : encore tiède
    (2001, 1000, "cold"),
    (None, 1000, "cold"),
    (1000, None, "cold"),
    (1000, 0, "cold"),
])
def test_the_zone_follows_the_distance_to_the_alert_ceiling(price, ceiling, zone):
    assert cote_zone(price, ceiling) == zone


def test_the_hot_zone_is_polled_every_six_hours_and_the_cold_one_every_week():
    assert timedelta(hours=6) == HOT_CADENCE
    assert timedelta(days=2) == WARM_CADENCE
    assert timedelta(days=7) == COLD_CADENCE
    assert timedelta(days=30) == HISTORY_CADENCE


def test_a_book_close_to_its_ceiling_gets_the_short_rhythm():
    cache = _cache()

    record_component(cache, "current", succeeded=True, now=NOW,
                     price_cents=1100, ceiling_cents=1000)

    assert cache.refresh_state["current"]["zone"] == "hot"
    assert component_due(cache, "current", NOW) == NOW + HOT_CADENCE


def test_a_book_far_above_its_ceiling_gets_the_long_rhythm():
    cache = _cache()

    record_component(cache, "current", succeeded=True, now=NOW,
                     price_cents=9000, ceiling_cents=1000)

    assert cache.refresh_state["current"]["zone"] == "cold"
    assert component_due(cache, "current", NOW) == NOW + COLD_CADENCE


def test_a_book_changes_zone_at_the_next_cote_reading():
    """La zone est recalculée à chaque relevé : c'est le voisinage qui décide."""
    cache = _cache()

    record_component(cache, "current", succeeded=True, now=NOW,
                     price_cents=9000, ceiling_cents=1000)
    record_component(cache, "current", succeeded=True, now=NOW + COLD_CADENCE,
                     price_cents=1500, ceiling_cents=1000)

    assert cache.refresh_state["current"]["zone"] == "warm"
    assert component_due(cache, "current", NOW + COLD_CADENCE) == (
        NOW + COLD_CADENCE + WARM_CADENCE
    )


def test_service_order_puts_hot_before_warm_before_cold():
    assert zone_rank("hot") < zone_rank("warm") < zone_rank("cold")


# --------------------------------------------------------------------------- #
# Deux composantes, deux échéances
# --------------------------------------------------------------------------- #


def test_the_two_components_have_separate_deadlines():
    cache = _cache()

    record_component(cache, "current", succeeded=True, now=NOW,
                     price_cents=1500, ceiling_cents=1000)
    record_component(cache, "history", succeeded=True, now=NOW)

    assert component_due(cache, "current", NOW) == NOW + WARM_CADENCE
    assert component_due(cache, "history", NOW) == NOW + HISTORY_CADENCE
    assert due_components(cache, NOW + WARM_CADENCE) == ("current",)


def test_an_unread_history_is_due_immediately():
    cache = _cache()

    record_component(cache, "current", succeeded=True, now=NOW,
                     price_cents=1500, ceiling_cents=1000)

    assert due_components(cache, NOW) == ("history",)
    assert collection_due(cache, NOW) is True


def test_a_never_read_book_is_due_now():
    assert due_components(_cache(), NOW) == ("current", "history")
    assert collection_due(_cache(), NOW) is True


def test_a_legacy_cache_is_read_through_its_own_columns():
    """Aucune cote en base n'est réécrite : les anciennes lignes se lisent pareil."""
    cache = _cache(
        last_successful_collection_at=NOW - timedelta(days=3),
        historical_checked_at=NOW - timedelta(days=10),
    )

    assert component_due(cache, "current", NOW) == NOW - timedelta(days=3) + COLD_CADENCE
    assert component_due(cache, "history", NOW) == NOW + timedelta(days=20)


def test_a_big_drop_makes_the_history_due_immediately():
    """Une baisse de plus de 30 % tend le filet anti-oubli le jour même."""
    cache = _cache()
    record_component(cache, "current", succeeded=True, now=NOW,
                     price_cents=5000, ceiling_cents=5000)
    record_component(cache, "history", succeeded=True, now=NOW)
    assert component_due(cache, "history", NOW) == NOW + HISTORY_CADENCE

    later = NOW + timedelta(hours=3)
    record_component(cache, "current", succeeded=True, now=later,
                     price_cents=3000, ceiling_cents=5000)

    assert component_due(cache, "history", later) == later
    assert "history" in due_components(cache, later)


def test_a_small_drop_does_not_trigger_a_history_reading():
    cache = _cache()
    record_component(cache, "current", succeeded=True, now=NOW,
                     price_cents=5000, ceiling_cents=5000)
    record_component(cache, "history", succeeded=True, now=NOW)

    later = NOW + timedelta(hours=3)
    record_component(cache, "current", succeeded=True, now=later,
                     price_cents=4000, ceiling_cents=5000)  # -20 %

    assert component_due(cache, "history", later) == NOW + HISTORY_CADENCE


def test_a_rise_does_not_trigger_a_history_reading():
    cache = _cache()
    record_component(cache, "current", succeeded=True, now=NOW,
                     price_cents=5000, ceiling_cents=5000)
    record_component(cache, "history", succeeded=True, now=NOW)

    later = NOW + timedelta(hours=3)
    record_component(cache, "current", succeeded=True, now=later,
                     price_cents=9000, ceiling_cents=5000)

    assert component_due(cache, "history", later) == NOW + HISTORY_CADENCE


# --------------------------------------------------------------------------- #
# La péremption à l'affichage
# --------------------------------------------------------------------------- #


def test_a_fresh_cote_is_shown_as_an_amount():
    cache = _cache(lowest_delivered_price_cents=1999)
    record_component(cache, "current", succeeded=True, now=NOW,
                     price_cents=1999, ceiling_cents=2000)

    assert cote_is_stale(cache, NOW + timedelta(hours=1)) is False
    assert fresh_cote_cents(cache, NOW + timedelta(hours=1)) == 1999


@pytest.mark.parametrize("zone_price", [900, 1500, 5000])
def test_a_cote_past_its_rhythm_and_the_tolerance_is_no_longer_an_amount(zone_price):
    cache = _cache(lowest_delivered_price_cents=zone_price)
    record_component(cache, "current", succeeded=True, now=NOW,
                     price_cents=zone_price, ceiling_cents=1000)

    cadence = cote_cadence(cache)
    just_before = NOW + cadence + DISPLAY_TOLERANCE
    just_after = just_before + timedelta(minutes=1)

    assert cote_is_stale(cache, just_before) is False
    assert cote_is_stale(cache, just_after) is True
    assert fresh_cote_cents(cache, just_after) is None
    assert cote_age(cache, just_after) == cadence + DISPLAY_TOLERANCE + timedelta(minutes=1)


def test_the_display_tolerance_is_forty_eight_hours():
    assert timedelta(hours=48) == DISPLAY_TOLERANCE


def test_a_cache_without_any_reading_is_not_stale_but_shows_nothing():
    """Sans relevé, il n'y a pas d'âge : l'absence de cote garde ses phrases."""
    cache = _cache(lowest_delivered_price_cents=1999)

    assert cote_age(cache, NOW) is None
    assert cote_is_stale(cache, NOW) is False
    assert fresh_cote_cents(cache, NOW) == 1999


# --------------------------------------------------------------------------- #
# La mesure du retard
# --------------------------------------------------------------------------- #


def test_a_late_book_is_out_of_rhythm_only_past_the_tolerance():
    cache = _cache()
    record_component(cache, "current", succeeded=True, now=NOW,
                     price_cents=1000, ceiling_cents=1000)
    record_component(cache, "history", succeeded=True, now=NOW)

    assert out_of_rhythm(cache, NOW + HOT_CADENCE) is False
    assert out_of_rhythm(cache, NOW + HOT_CADENCE + DISPLAY_TOLERANCE) is False
    assert out_of_rhythm(cache, NOW + HOT_CADENCE + DISPLAY_TOLERANCE
                         + timedelta(minutes=1)) is True


def test_a_book_without_cache_is_never_counted_out_of_rhythm():
    assert out_of_rhythm(None, NOW) is False


# --------------------------------------------------------------------------- #
# Le plafond de l'alerte nourrit la zone
# --------------------------------------------------------------------------- #


def test_the_stamp_reads_the_zone_from_the_alert_ceiling():
    """La cote relevée prend le rythme de son voisinage, pas un rythme par défaut."""
    from app.providers.base import CollectionAttempt, MarketOffer, MarketOffers
    from app.services.book_market_pricing import BookPricingResult, _stamp_collection

    offer = MarketOffer(
        provider_name="chasse_aux_livres", item_price_cents=1100,
        shipping_price_cents=None, delivered_price_cents=1100,
        source_url="https://chasse.example/1100", item_condition="Très bon état",
        is_comparable=True, is_available=True, price_includes_shipping=True,
    )
    result = BookPricingResult.from_offers(
        MarketOffers([offer], attempts=(CollectionAttempt("chasse_aux_livres", "current"),)),
        offer, None,
    )
    cache = _cache()

    _stamp_collection(cache, result, 1000)

    assert cache.refresh_state["current"]["zone"] == "hot"
    assert component_due(cache, "current", result.collected_at) == (
        result.collected_at + HOT_CADENCE
    )


def test_the_same_cote_far_from_the_ceiling_waits_a_week():
    from app.providers.base import CollectionAttempt, MarketOffer, MarketOffers
    from app.services.book_market_pricing import BookPricingResult, _stamp_collection

    offer = MarketOffer(
        provider_name="chasse_aux_livres", item_price_cents=9000,
        shipping_price_cents=None, delivered_price_cents=9000,
        source_url="https://chasse.example/9000", item_condition="Très bon état",
        is_comparable=True, is_available=True, price_includes_shipping=True,
    )
    result = BookPricingResult.from_offers(
        MarketOffers([offer], attempts=(CollectionAttempt("chasse_aux_livres", "current"),)),
        offer, None,
    )
    cache = _cache()

    _stamp_collection(cache, result, 1000)

    assert cache.refresh_state["current"]["zone"] == "cold"
    assert component_due(cache, "current", result.collected_at) == (
        result.collected_at + COLD_CADENCE
    )
