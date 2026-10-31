"""Une passe ne parle que pour un fait actionnable ; le reste est silence.

Le bilan de passe à quatre compteurs a disparu, avec ses modes « incrémental » et
« complet » : aucun de ses nombres ne permettait de décider quoi que ce soit. Ce
qui reste, c'est le fait — une source durablement en panne, son retour durable,
une passe interrompue par une exception —, et rien d'autre.

La durabilité est le contrat de ce fichier : trois observations consécutives
couvrant au moins une heure, dans les deux sens. Un échantillon en échec ne vaut
jamais un incident, et une alternance échec/succès ne dit jamais rien.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.providers.base import CollectionAttempt
from app.services import market_health

SOURCE = "chasse_aux_livres"


def _failed(reason: str = "timeout", operation: str = "current") -> CollectionAttempt:
    return CollectionAttempt(SOURCE, operation, "failed", reason)


def _answered(operation: str = "current", *, status: str = "success",
              reason: str | None = None, http_status: int | None = 200):
    return CollectionAttempt(SOURCE, operation, status, reason, http_status)


@pytest.fixture
def notifier(monkeypatch):
    monkeypatch.setattr(market_health, "_incident_sources", {})
    monkeypatch.setattr(market_health, "_incident_announced", set())
    monkeypatch.setattr(market_health, "_incident_pending", {})
    monkeypatch.setattr(market_health, "_incident_loaded", False)
    monkeypatch.setattr(market_health, "_notification_deadlines", {})
    client = AsyncMock()
    client.get.return_value = None
    client.set.return_value = True
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(market_health, "notify_admins", sent)
    return sent


def result(*, occurrence="pass-1", attempts=(), errors=0):
    return market_health.RefreshPassResult(
        total=3,
        stats={
            "processed": 3,
            "refreshed": 0,
            "available": 0,
            "unavailable": 0,
            "degraded": 3,
            "errors": errors,
        },
        occurrence=occurrence,
        attempts=list(attempts),
    )


async def observe(market_clock, *, attempts=(), minutes=30, errors=0):
    """Joue une passe `minutes` après la précédente."""
    market_clock.advance(minutes)
    await market_health.notify_refresh_result(result(attempts=attempts, errors=errors))


@pytest.mark.asyncio
async def test_a_single_failing_pass_never_speaks(notifier, market_clock):
    """Contrat : un passage en échec est un accident, pas une panne de source."""
    await observe(market_clock, attempts=[_failed()])
    await observe(market_clock, attempts=[_failed(), _failed()], minutes=1)
    assert notifier.await_count == 0
    assert market_health._incident_sources[f"{SOURCE}/current"].outcome == market_health.FAILED


@pytest.mark.asyncio
async def test_an_incident_needs_failures_spread_over_an_hour(notifier, market_clock):
    """Contrat : l'incident dit la source, l'opération, la cause et l'heure de début."""
    await observe(market_clock, attempts=[_failed("rate_limited")])
    await observe(market_clock, attempts=[_failed("bot_challenge")])
    assert notifier.await_count == 0, "deux passes rapprochées ne suffisent pas"
    await observe(market_clock, attempts=[_failed("bot_challenge")])
    assert notifier.await_count == 1
    text = notifier.call_args.args[0]
    assert text.startswith(f"Nouvel incident : {SOURCE}/current — en échec depuis ")
    assert "quota refusé (HTTP 429), défi anti-robot" in text
    assert text.count("\n") == 0
    for counter in ("Actualisées", "Traités", "Jamais collectés", "Non parcourus",
                    "Restants à traiter", "incrémental", "complet"):
        assert counter not in text


@pytest.mark.asyncio
async def test_alternating_success_and_failure_never_speaks(notifier, market_clock):
    """Le cœur du défaut mesuré : échec, succès, échec… ne doit jamais parler.

    Vingt-quatre passes, une toutes les trente minutes : douze échecs et douze
    succès qui s'alternent. La règle livrée annonçait un incident au premier échec
    puis un rétablissement au premier succès, soit douze paires en douze heures.
    """
    for index in range(24):
        awaits = [_failed("timeout")] if index % 2 == 0 else [_answered()]
        await observe(market_clock, attempts=awaits)
    assert notifier.await_count == 0
    assert not market_health._incident_announced


@pytest.mark.asyncio
async def test_a_budget_partial_is_an_incomplete_reading_not_an_incident(notifier, market_clock):
    """Mesure du 31 octobre : `incomplete_offers` arrive en HTTP 200, source debout."""
    for _ in range(8):
        await observe(market_clock, attempts=[
            _answered(status="partial", reason="incomplete_offers", http_status=200),
        ])
    assert notifier.await_count == 0


@pytest.mark.asyncio
async def test_a_book_level_failure_is_not_a_source_failure(notifier, market_clock):
    """Le 31 octobre, l'incident réel venait de deux `volume_mismatch` : mauvais tome."""
    for reason in ("volume_mismatch", "identity_mismatch", "empty_response"):
        for _ in range(4):
            await observe(market_clock, attempts=[_failed(reason)])
    assert notifier.await_count == 0


@pytest.mark.asyncio
async def test_a_very_long_lookup_is_not_a_broken_source(notifier, market_clock):
    """Une source qui répond une fois sur deux n'est pas en panne : elle est lente."""
    for _ in range(12):
        await observe(market_clock, attempts=[_failed("timeout"), _answered()])
    assert notifier.await_count == 0


@pytest.mark.asyncio
async def test_a_recovery_needs_the_same_persistence_as_the_incident(notifier, market_clock):
    """Contrat : une seule passe propre après un incident ne suffit pas."""
    for _ in range(3):
        await observe(market_clock, attempts=[_failed()])
    assert notifier.await_count == 1
    await observe(market_clock, attempts=[_answered()])
    await observe(market_clock, attempts=[_answered()])
    assert notifier.await_count == 1, "deux réponses rapprochées ne rétablissent pas"
    await observe(market_clock, attempts=[_answered()])
    assert notifier.await_count == 2
    text = notifier.call_args.args[0]
    assert text.startswith(f"Rétablissement : {SOURCE}/current — la source répond depuis ")
    await observe(market_clock, attempts=[_answered()])
    assert notifier.await_count == 2, "un rétablissement ne s'annonce qu'une fois"


@pytest.mark.asyncio
async def test_an_announced_incident_stays_silent_while_it_lasts(notifier, market_clock):
    for _ in range(3):
        await observe(market_clock, attempts=[_failed()])
    for _ in range(6):
        await observe(market_clock, attempts=[_failed("http_status")])
    assert notifier.await_count == 1


@pytest.mark.asyncio
async def test_a_source_that_stops_appearing_leaves_without_a_message(notifier, market_clock):
    """Règle d'oubli : un couple plus tenté depuis la fenêtre sort de l'état, muet."""
    for _ in range(3):
        await observe(market_clock, attempts=[_failed()])
    assert market_health._incident_announced == {f"{SOURCE}/current"}
    for _ in range(4):
        await observe(market_clock, attempts=[], minutes=180)
    assert f"{SOURCE}/current" not in market_health._incident_sources
    assert not market_health._incident_announced
    assert notifier.await_count == 1


@pytest.mark.asyncio
async def test_an_unavailable_source_is_not_an_incident_by_absence(notifier, market_clock):
    """Une source reportée par le régulateur n'est pas observée, donc pas jugée."""
    await observe(market_clock, attempts=[_failed()])
    for _ in range(6):
        await observe(market_clock, attempts=[
            CollectionAttempt(SOURCE, "current", "skipped", "pass_budget"),
        ])
    assert notifier.await_count == 0
    assert market_health._incident_sources[f"{SOURCE}/current"].samples == 1


@pytest.mark.asyncio
async def test_a_durable_processing_error_is_named_without_a_counter(notifier, market_clock):
    for _ in range(3):
        await observe(market_clock, attempts=[], errors=1)
    assert notifier.await_count == 1
    assert "Nouvel incident : traitement" in notifier.call_args.args[0]
    assert "Traités :" not in notifier.call_args.args[0]


@pytest.mark.asyncio
async def test_an_incident_is_logged_with_the_window_that_justified_it(market_clock, caplog):
    """« Pourquoi ce message ? » doit rester répondable dans les journaux."""
    with caplog.at_level("WARNING"):
        for _ in range(3):
            await observe(market_clock, attempts=[_failed("rate_limited")])
    records = [
        record for record in caplog.records
        if record.message == "Incident de source annoncé"
    ]
    assert len(records) == 1
    extra = records[0].__dict__
    assert extra["component"] == f"{SOURCE}/current"
    assert extra["samples"] == 3
    assert extra["span_seconds"] == 3600
    assert extra["causes"] == ["rate_limited"]


@pytest.mark.asyncio
async def test_an_undelivered_incident_is_retried_until_delivered(notifier, market_clock):
    notifier.side_effect = [False, True]
    for _ in range(3):
        await observe(market_clock, attempts=[_failed()])
    assert notifier.await_count == 1
    assert f"{SOURCE}/current" in market_health._incident_pending
    await observe(market_clock, attempts=[_failed()])
    assert notifier.await_count == 2
    assert market_health._incident_announced == {f"{SOURCE}/current"}
    assert market_health._incident_pending == {}
    await observe(market_clock, attempts=[_failed()])
    assert notifier.await_count == 2, "un incident remis ne se répète pas"


@pytest.mark.asyncio
async def test_an_undelivered_incident_and_its_recovery_are_delivered_together(
    notifier, market_clock,
):
    """Un incident non remis n'efface pas son rétablissement, ni l'inverse."""
    notifier.side_effect = [False, False, False, False, True]
    for _ in range(3):
        await observe(market_clock, attempts=[_failed()])
    for _ in range(4):
        await observe(market_clock, attempts=[_answered()])
    assert notifier.await_count == 5
    lines = notifier.call_args.args[0].splitlines()
    assert lines[0].startswith(f"Nouvel incident : {SOURCE}/current")
    assert lines[1].startswith(f"Rétablissement : {SOURCE}/current")
    assert not market_health._incident_announced
    assert market_health._incident_pending == {}


@pytest.mark.asyncio
async def test_the_state_survives_a_restart_through_redis(monkeypatch, market_clock):
    stored: dict[str, str] = {}

    async def read(key):
        return stored.get(key)

    async def write(key, value, **_kwargs):
        stored[key] = value
        return True

    def restart():
        monkeypatch.setattr(market_health, "_incident_sources", {})
        monkeypatch.setattr(market_health, "_incident_announced", set())
        monkeypatch.setattr(market_health, "_incident_pending", {})
        monkeypatch.setattr(market_health, "_incident_loaded", False)
        monkeypatch.setattr(market_health, "_incident_lock", market_health.asyncio.Lock())
        monkeypatch.setattr(market_health, "get_redis_client", lambda: type(
            "Client", (), {"get": AsyncMock(side_effect=read), "set": AsyncMock(side_effect=write)},
        )())
        sent = AsyncMock(return_value=True)
        monkeypatch.setattr(market_health, "notify_admins", sent)
        return sent

    sent = restart()
    await observe(market_clock, attempts=[_failed()])
    await observe(market_clock, attempts=[_failed()])
    sent = restart()
    await observe(market_clock, attempts=[_failed()])
    assert sent.await_count == 1, "la troisième passe conclut après un redémarrage"
    sent = restart()
    await observe(market_clock, attempts=[_failed()])
    assert sent.await_count == 0, "l'incident annoncé ne se répète pas"
    for _ in range(3):
        await observe(market_clock, attempts=[_answered()])
    assert sent.await_count == 1
    assert "Rétablissement" in sent.call_args.args[0]


@pytest.mark.asyncio
async def test_a_state_of_the_previous_format_is_never_resurrected(monkeypatch, market_clock):
    """L'ancien état ne contenait aucune observation datée : rien à en garder."""
    monkeypatch.setattr(market_health, "_incident_sources", {})
    monkeypatch.setattr(market_health, "_incident_announced", set())
    monkeypatch.setattr(market_health, "_incident_pending", {})
    monkeypatch.setattr(market_health, "_incident_loaded", False)
    client = AsyncMock()
    client.get.return_value = (
        '{"observed": {"momox/current": {"ratio": 1.0, "count": 5.0}},'
        ' "notified": {"momox/current": {"ratio": 1.0, "count": 5.0}}, "pending": {}}'
    )
    monkeypatch.setattr(market_health, "get_redis_client", lambda: client)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(market_health, "notify_admins", sent)
    await observe(market_clock, attempts=[_answered()])
    assert sent.await_count == 0
    assert "momox/current" not in market_health._incident_sources
    assert not market_health._incident_announced


@pytest.mark.asyncio
async def test_a_pass_without_a_single_fact_says_nothing(notifier, market_clock):
    """Contrat : zéro livre traité et rien à signaler, c'est une passe qui se tait."""
    report = result()
    report.total = 0
    report.stats = dict.fromkeys(report.stats, 0)
    market_clock.advance(30)
    await market_health.notify_refresh_result(report)
    assert notifier.await_count == 0


@pytest.mark.asyncio
async def test_selection_exception_is_reported_and_still_raised(notifier):
    from app.tasks import refresh_book_prices

    with (
        patch.object(
            refresh_book_prices,
            "_select_books",
            AsyncMock(side_effect=RuntimeError("synthetic")),
        ),
        patch.object(refresh_book_prices, "read_refresh_health", AsyncMock()),
        pytest.raises(
            RuntimeError,
            match="synthetic",
        ),
    ):
        await refresh_book_prices.refresh_book_prices_task(
            registry=AsyncMock(), session=AsyncMock()
        )
    assert notifier.await_count == 1
    # Contrat : l'interruption par exception est un fait, et c'est la seule ligne.
    assert notifier.call_args.args[0] == (
        "⚠️ Passe de rafraîchissement interrompue avant la fin."
    )


@pytest.mark.asyncio
async def test_a_pass_cut_by_the_ceiling_is_the_normal_case_and_is_silent(notifier, market_clock):
    """Contrat : une passe coupée par le plafond de durée ne dit plus rien du tout.

    Le message partait deux fois par jour pour décrire un fonctionnement prévu ;
    la durée d'une passe n'est pas un fait de marché.
    """
    report = result()
    report.interrupted = True
    report.stats["degraded"] = 0
    market_clock.advance(30)
    await market_health.notify_refresh_result(report)
    assert notifier.await_count == 0


def test_current_offer_counts_as_refreshed_when_history_fails():
    from app.db.models.book_market_cache import BookMarketCache
    from app.providers.base import MarketOffer, MarketOffers
    from app.services.book_market_pricing import BookPricingResult
    from app.tasks.refresh_book_prices import _book_result

    offer = MarketOffer(
        provider_name="source",
        item_price_cents=2059,
        shipping_price_cents=0,
        delivered_price_cents=2059,
        source_url="https://example.invalid",
    )
    offers = MarketOffers(
        [offer],
        failed_providers=("source",),
        attempts=(
            CollectionAttempt("source"),
            CollectionAttempt("source", "history", "failed", "timeout"),
        ),
    )
    result = BookPricingResult.from_offers(offers, offer, None)
    outcome = _book_result(BookMarketCache(collection_status="partial"), result)
    assert outcome.refreshed
    assert outcome.verdict == "degraded"
    assert outcome.attempts[0].status == "success"
    assert outcome.attempts[1].operation == "history"


def test_reused_cache_does_not_count_as_a_new_observation():
    from app.db.models.book_market_cache import BookMarketCache
    from app.domain.enums import ComparisonStatus
    from app.tasks.refresh_book_prices import _book_result

    cached = BookMarketCache(
        collection_status="complete", comparison_status=ComparisonStatus.AVAILABLE
    )
    assert not _book_result(cached, None).refreshed
