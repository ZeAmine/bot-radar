"""Péremption affichée : une cote trop vieille dit son âge, jamais son montant.

Un prix qu'on ne peut pas garantir ne se présente pas comme garanti. Au-delà du
rythme de sa zone plus la tolérance d'affichage de 48 h, la cote quitte donc la
forme « montant » pour la forme « non vérifié depuis N jours (relevé du …) », et
cette forme est la même sur tous les écrans. Ce que ces tests verrouillent :

- le seuil de bascule suit le rythme de la zone, pas une constante unique ;
- la phrase se construit **uniquement** dans `app/bot/labels.py` ;
- la ligne ne porte plus ni montant, ni marchand, ni emoji décoratif ;
- le prix bas sur douze mois, lui, garde toujours son montant : c'est un
  historique daté, pas une cote du jour.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.bot.labels import FIELD_LAST_OFFER, cote_line, format_age, format_day
from app.core.constants import ComparisonStatus
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.domain.rules.cote_outcome import OffreCote, select_cote_offer
from app.services import market_refresh_policy as policy
from app.services.market_price_service import (
    MarketPriceService,
    cache_cote_outcomes,
    format_cote_lines,
)
from app.services.market_refresh_policy import (
    COLD_CADENCE,
    DISPLAY_TOLERANCE,
    HOT_CADENCE,
    WARM_CADENCE,
    cote_display,
    cote_is_stale,
    cote_zone,
    record_component,
)
from tests.fakes_market_source import attach_source_history

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
CEILING = 2000


def _book(
    *, now: datetime = NOW, price_cents: int | None = 1900, ceiling: int | None = CEILING,
) -> Book:
    """Un livre avec une cote relevée à `now`, dans la zone que son prix lui donne."""
    book = Book(
        id=uuid.uuid4(),
        title="Blacksad — Intégrale",
        normalized_title="blacksad integrale",
        isbn13="9782205078054",
    )
    cache = BookMarketCache(
        id=uuid.uuid4(),
        book_id=book.id,
        lowest_delivered_price_cents=price_cents,
        best_provider_name="momox",
        comparison_status=ComparisonStatus.AVAILABLE,
        checked_at=now,
        expires_at=now + timedelta(days=365),
        annual_low_cents=1200,
        annual_low_observed_at=now,
        annual_computed_at=now,
        annual_expires_at=now + timedelta(days=365),
    )
    attach_source_history(cache, 1200, now)
    if price_cents is not None:
        record_component(
            cache, "current", succeeded=True, now=now,
            price_cents=price_cents, ceiling_cents=ceiling,
        )
        record_component(cache, "history", succeeded=True, now=now)
        cache.last_successful_collection_at = now
    book.market_cache = cache
    return book


def _age_the_cote(book: Book, age: timedelta, ceiling: int | None = CEILING) -> None:
    """Vieillit la cote sans toucher au reste : c'est l'échéance qui compte.

    Le plafond est celui du livre, pas une constante : c'est lui qui choisit la
    zone, donc le rythme, donc l'âge à partir duquel la cote expire.
    """
    refreshed_at = NOW - age
    cache = book.market_cache
    assert cache is not None
    record_component(
        cache, "current", succeeded=True, now=refreshed_at,
        price_cents=cache.lowest_delivered_price_cents, ceiling_cents=ceiling,
    )
    cache.last_successful_collection_at = refreshed_at
    cache.checked_at = refreshed_at


def _cote_line_of(book: Book, now: datetime = NOW) -> str:
    lines = format_cote_lines(book, now=now)
    assert lines, "une cote connue produit toujours au moins sa ligne"
    return lines[0]


# --------------------------------------------------------------------------- #
# Le seuil de bascule suit le rythme de la zone
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("price", "ceiling", "cadence"), [
    (1100, 1000, HOT_CADENCE),     # chaud : 6 h
    (1500, 1000, WARM_CADENCE),    # tiède : 2 jours
    (9000, 1000, COLD_CADENCE),    # froid : 7 jours
])
def test_an_expired_cote_becomes_an_age_exactly_past_its_rhythm_plus_the_tolerance(
    price: int, ceiling: int, cadence: timedelta,
) -> None:
    """Le seuil n'est pas une constante : chaud, tiède et froid ne périment pas ensemble."""
    fresh = _book(price_cents=price, ceiling=ceiling)
    _age_the_cote(fresh, cadence + DISPLAY_TOLERANCE - timedelta(minutes=1), ceiling)
    assert "€" in _cote_line_of(fresh)

    expired = _book(price_cents=price, ceiling=ceiling)
    _age_the_cote(expired, cadence + DISPLAY_TOLERANCE + timedelta(minutes=1), ceiling)
    assert "€" not in _cote_line_of(expired)


def test_the_tolerance_is_the_validated_forty_eight_hours() -> None:
    """La tolérance retenue par le capitaine est un fait du modèle, pas un réglage caché."""
    assert timedelta(hours=48) == DISPLAY_TOLERANCE


def test_the_zone_of_the_last_reading_decides_the_deadline() -> None:
    """Un livre proche de son plafond périme plus vite qu'un livre lointain."""
    near = _book(price_cents=1100, ceiling=1000)
    far = _book(price_cents=9000, ceiling=1000)
    _age_the_cote(near, timedelta(days=3), 1000)
    _age_the_cote(far, timedelta(days=3), 1000)

    assert cote_zone(near.market_cache.lowest_delivered_price_cents, 1000) == "hot"
    assert "€" not in _cote_line_of(near)
    assert "€" in _cote_line_of(far)


# --------------------------------------------------------------------------- #
# La phrase : un âge, une date, aucun montant
# --------------------------------------------------------------------------- #


def test_the_expired_line_names_the_age_and_the_date_of_the_last_reading() -> None:
    """« Non vérifié depuis N jours » sans date obligerait à soustraire des jours."""
    book = _book()
    _age_the_cote(book, timedelta(days=9))

    assert _cote_line_of(book) == (
        "<b>Dernière offre connue :</b> non vérifié depuis 9 jours "
        "(relevé du 22 octobre 2026)"
    )


def test_the_expired_line_keeps_no_amount_no_merchant_and_no_decorative_emoji() -> None:
    """Un prix qu'on ne garantit plus ne se qualifie plus : le marchand part avec lui."""
    book = _book()
    _age_the_cote(book, timedelta(days=9))
    line = _cote_line_of(book)

    assert "19,00 €" not in line
    assert "Momox" not in line
    assert "€" not in line
    assert not any(char in line for char in "⚠️🔍📉💸")


def test_the_label_of_the_line_lives_only_in_the_labels_module() -> None:
    """Le libellé est un mot du produit : il n'est écrit qu'à un endroit."""
    assert FIELD_LAST_OFFER == "Dernière offre connue"
    assert policy.__doc__ is not None


def test_an_age_in_readable_words_never_counts_hours_for_a_multi_day_gap() -> None:
    """« depuis 2 jours 3 heures » ne se lit pas ; « depuis 2 jours » si."""
    assert format_age(timedelta(hours=5)) == "moins d'un jour"
    assert format_age(timedelta(days=1, hours=2)) == "1 jour"
    assert format_age(timedelta(days=9, hours=23)) == "9 jours"
    assert format_age(timedelta(days=75)) == "2 mois"


def test_the_date_is_read_in_paris_time_not_in_utc() -> None:
    """Un relevé de 23 h 30 UTC est daté du lendemain à Paris : la date doit suivre."""
    assert format_day(datetime(2026, 10, 7, 23, 30, tzinfo=UTC)) == "8 octobre 2026"


def test_a_missing_reading_forbids_inventing_an_age() -> None:
    """Sans date connue, la phrase dit ce qu'elle ignore au lieu de compter faux."""
    cache = BookMarketCache(
        book_id=uuid.uuid4(),
        lowest_delivered_price_cents=1900,
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    display = cote_display(cache, NOW)

    assert display.stale is False
    assert display.age is None
    assert display.since is None


# --------------------------------------------------------------------------- #
# Tous les écrans disent la même chose
# --------------------------------------------------------------------------- #


def test_the_market_card_and_the_cote_lines_say_the_same_sentence() -> None:
    """Un même livre ne peut pas être périmé dans la fiche et valide dans le panneau."""
    book = _book()
    _age_the_cote(book, timedelta(days=9))

    from_lines = _cote_line_of(book)
    from_card = MarketPriceService.format_market_details_card(None, book, now=NOW)
    from_summary = MarketPriceService._market_price_line(book.market_cache, "", NOW)

    assert from_lines == from_summary
    assert from_lines in from_card


def test_a_fresh_cote_keeps_its_amount_and_its_merchant() -> None:
    """La forme montant n'est pas perdue : elle reste celle d'une cote à son rythme."""
    book = _book()
    _age_the_cote(book, timedelta(hours=1))

    assert _cote_line_of(book) == "<b>Dernière offre connue :</b> 19,00 €"
    assert MarketPriceService._market_price_line(
        book.market_cache, "", NOW,
    ) == "<b>Dernière offre connue :</b> 19,00 € (Momox)"


def test_the_twelve_month_low_keeps_its_amount_even_when_the_cote_expires() -> None:
    """Le prix bas est un historique daté : il ne périme pas avec la cote du jour."""
    book = _book()
    _age_the_cote(book, timedelta(days=9))
    card = MarketPriceService.format_market_details_card(None, book, now=NOW)

    assert "Prix bas :</b> 12,00 €" in card
    assert "€" not in _cote_line_of(book)


def test_a_retained_offer_is_quoted_as_an_age_when_it_has_expired() -> None:
    """Le prix retenu documente l'absence ; périmé, il ne s'y présente plus en montant."""
    book = _book()
    _age_the_cote(book, timedelta(days=9))
    cote, _ = cache_cote_outcomes(book)

    line = cote_line(cote)
    assert "récupération impossible" in line or "aucune offre" in line
    assert "(non vérifié depuis 9 jours, relevé du 22 octobre 2026)" in line
    assert "19,00 €" not in line


def test_a_cote_read_back_from_the_cache_expires_like_a_collected_one() -> None:
    """L'aperçu d'alerte relit la cote en base : une cote de neuf jours y périme aussi."""
    book = _book()
    cache = book.market_cache
    assert cache is not None
    cache.collection_status = "success"
    cache.current_offers = [{
        "provider_name": "momox",
        "item_price_cents": 1900,
        "shipping_price_cents": 0,
        "delivered_price_cents": 1900,
        "source_url": "https://www.momox.fr/offre/1",
        "condition": "TBE",
        "is_secondhand": True,
    }]
    _age_the_cote(book, timedelta(days=9))
    cote, _ = cache_cote_outcomes(book)

    # La décision ne change pas : la cote existe toujours.
    assert cote.state == "cote_trouvee"
    assert cote.found is True
    assert cote_line(cote) == (
        "<b>Cote :</b> non vérifié depuis 9 jours (relevé du 22 octobre 2026)"
    )


def test_a_cache_cote_that_is_still_fresh_keeps_its_amount_and_its_merchant() -> None:
    """La péremption ne doit pas se déclencher sur une cote qui tient son rythme."""
    book = _book()
    cache = book.market_cache
    assert cache is not None
    cache.collection_status = "success"
    cache.current_offers = [{
        "provider_name": "momox",
        "item_price_cents": 1900,
        "shipping_price_cents": 0,
        "delivered_price_cents": 1900,
        "source_url": "https://www.momox.fr/offre/1",
        "condition": "TBE",
        "is_secondhand": True,
    }]
    _age_the_cote(book, timedelta(hours=1))
    cote, _ = cache_cote_outcomes(book)

    assert cote_line(cote) == "<b>Cote :</b> 19,00 € (Momox)"


def test_selecting_an_offer_ignores_the_age_of_the_previous_one() -> None:
    """Une offre du jour n'a pas d'âge à déclarer : elle a sa propre collecte."""
    outcome = select_cote_offer([OffreCote(marchand="Momox", etat="TBE", total_cents=2369)])

    assert outcome.state == "cote_trouvee"
    assert "non vérifié" not in cote_line(outcome)


def test_a_stale_cache_is_reported_as_past_its_display_deadline() -> None:
    """Le modèle expose la péremption d'affichage, l'écran ne la recalcule pas."""
    book = _book()
    _age_the_cote(book, HOT_CADENCE + DISPLAY_TOLERANCE + timedelta(minutes=1))

    assert cote_is_stale(book.market_cache, NOW) is True
    assert cote_display(book.market_cache, NOW).stale is True
