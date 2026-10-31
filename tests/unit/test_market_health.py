"""Le signal qui dit qu'une cote n'est plus nourrie, testé sans réseau ni base."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import market_health
from app.services.market_health import (
    StaleCertifiedLow,
    format_stale_certified_lows,
    notify_stale_certified_lows,
)


def _item(days_ago: int | None, title: str = "Dead Train", cents: int = 1357):
    last_seen = None if days_ago is None else datetime.now(UTC) - timedelta(days=days_ago)
    return StaleCertifiedLow(
        title=title,
        isbn13="9782380463972",
        low_cents=cents,
        last_seen=last_seen,
    )


def test_message_says_what_is_frozen_and_for_how_long():
    """Un chiffre sans âge est un chiffre qu'on ne peut pas juger."""
    message = format_stale_certified_lows([_item(23), _item(None, title="Carcajou", cents=390)])

    assert "Dead Train" in message
    assert "13,57 €" not in message
    assert "rien depuis 23 j" in message
    assert "jamais observée" in message
    assert "3,90 €" not in message
    assert "Historique fournisseur non actualisé" in message
    assert "Agrégats historiques" not in message


def test_no_stale_book_means_no_message():
    """Un contrôle quotidien qui parle de tout parle de rien."""
    assert format_stale_certified_lows([]) == ""


def test_the_message_counts_what_the_list_leaves_out():
    """Une liste tronquée ne doit pas devenir le décompte annoncé.

    Huit lignes pour cent onze livres figés feraient passer l'incident pour une
    sélection : le chiffre du message est celui de l'inventaire, pas celui de
    l'extrait.
    """
    items = [_item(23, title=f"Book {i:02d}") for i in range(8)]

    message = format_stale_certified_lows(items, total=111)

    assert "111 livre(s)" in message
    assert "… et 103 autre(s)" in message


def test_a_full_list_that_is_the_whole_inventory_states_no_remainder():
    """Rien à annoncer au-delà quand la liste contient déjà tout."""
    items = [_item(23, title=f"Book {i:02d}") for i in range(3)]

    message = format_stale_certified_lows(items, total=3)

    assert "3 livre(s)" in message
    assert "autre(s)" not in message


@pytest.mark.asyncio
async def test_notification_is_sent_once_per_window():
    """Le job passe deux fois par jour : le signal ne doit pas se répéter."""
    repo_result = AsyncMock(return_value=([("Dead Train", "9782380463972", 1357, None)], 1))

    with (
        pytest.MonkeyPatch.context() as mp,
    ):
        mp.setattr(
            market_health.MarketPriceRepository,
            "find_stale_certified_lows",
            repo_result,
        )
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        client = MagicMock()
        client.set = AsyncMock(side_effect=[True, False])
        mp.setattr(market_health, "get_redis_client", lambda: client)

        first = await notify_stale_certified_lows(MagicMock())
        second = await notify_stale_certified_lows(MagicMock())

    assert first == 1
    assert second == 0
    assert len(sent) == 1
    assert client.set.await_args.kwargs["nx"] is True


@pytest.mark.asyncio
async def test_nothing_is_sent_when_every_book_is_fresh():
    """Une base saine ne doit jamais notifier."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            market_health.MarketPriceRepository,
            "find_stale_certified_lows",
            AsyncMock(return_value=([], 0)),
        )
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        count = await notify_stale_certified_lows(MagicMock())

    assert count == 0
    assert sent == []


@pytest.mark.asyncio
async def test_a_broken_health_check_never_breaks_the_refresh():
    """Le contrôle surveille le job ; il ne doit pas pouvoir le faire tomber."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            market_health.MarketPriceRepository,
            "find_stale_certified_lows",
            AsyncMock(side_effect=RuntimeError("base injoignable")),
        )

        assert await notify_stale_certified_lows(MagicMock()) == 0


@pytest.mark.asyncio
async def test_a_silent_redis_does_not_silence_the_signal():
    """Mieux vaut deux messages qu'un silence : le dédoublonnage est un confort."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            market_health.MarketPriceRepository,
            "find_stale_certified_lows",
            AsyncMock(return_value=([("Dead Train", "9782380463972", 1357, None)], 1)),
        )
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)
        client = MagicMock()
        client.set = AsyncMock(side_effect=RuntimeError("redis muet"))
        mp.setattr(market_health, "get_redis_client", lambda: client)

        assert await notify_stale_certified_lows(MagicMock()) == 1

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_silent_provider_is_reported_once_per_window():
    """Une source qui cesse de répondre ne se signale pas d'elle-même."""
    now = datetime.now(UTC)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            market_health.MarketPriceRepository,
            "find_silent_providers",
            AsyncMock(return_value=[("momox", now - timedelta(hours=200))]),
        )
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)
        client = MagicMock()
        client.set = AsyncMock(side_effect=[True, False])
        mp.setattr(market_health, "get_redis_client", lambda: client)

        first = await market_health.notify_silent_providers(MagicMock())
        second = await market_health.notify_silent_providers(MagicMock())

    assert first == 1
    assert second == 0
    assert len(sent) == 1
    assert "momox" in sent[0]
    assert "192 h" in sent[0]
    assert "200 h" in sent[0]


@pytest.mark.asyncio
async def test_every_provider_answered_means_no_message():
    """Une base saine ne doit jamais notifier."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            market_health.MarketPriceRepository,
            "find_silent_providers",
            AsyncMock(return_value=[]),
        )
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        count = await market_health.notify_silent_providers(MagicMock())

    assert count == 0
    assert sent == []


@pytest.mark.asyncio
async def test_a_broken_silent_check_never_breaks_the_refresh():
    """Le contrôle surveille le job ; il ne doit pas pouvoir le faire tomber."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            market_health.MarketPriceRepository,
            "find_silent_providers",
            AsyncMock(side_effect=RuntimeError("base injoignable")),
        )

        assert await market_health.notify_silent_providers(MagicMock()) == 0


def test_classify_sent_cote_state_names_the_five_outcomes():
    """La mesure doit distinguer une cote chiffrée des quatre lignes sans prix.

    L'absence constatée compte à part de l'attente : la compter ``pending``
    reviendrait à attendre une cote que la passe a déjà rendue vide. La série
    que personne ne catalogue compte à part elle aussi : sans source, aucune
    passe ne la cotera jamais, et la compter ``searching`` ferait attendre une
    recherche qui est terminée (ADR-041).
    """
    from app.services.market_health import classify_sent_cote_state
    from app.services.market_price_service import (
        NO_CATALOGUE_LINE,
        NO_OFFER_LINE,
        PENDING_COTE_LINE,
        SEARCHING_BOOK_LINE,
    )

    assert classify_sent_cote_state("<b>Prix bas :</b> 19,98 €") == "real"
    assert classify_sent_cote_state(PENDING_COTE_LINE) == "pending"
    assert classify_sent_cote_state(SEARCHING_BOOK_LINE) == "searching"
    # La ligne d'absence est datée : sa phrase l'identifie, pas toute la ligne.
    assert classify_sent_cote_state(f"{NO_OFFER_LINE} (relevé du 06/10 à 11:30).") == "no_offer"
    # Non catalogué : ni recherche en cours, ni cote chiffrée.
    assert classify_sent_cote_state(NO_CATALOGUE_LINE) == "uncatalogued"
    # Une ligne absente est un livre dont la cote manque, pas une recherche qui n'a pas lieu.
    assert classify_sent_cote_state(None) == "pending"


@pytest.mark.asyncio
async def test_recording_a_cote_state_counts_it_without_raising():
    """La mesure incrémente le bon compteur, et tous les états sont lisibles."""
    client = MagicMock()
    client.incr = AsyncMock()
    client.mget = AsyncMock(return_value=["5", None, "3", "2", "4", "1"])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(market_health, "get_redis_client", lambda: client)
        await market_health.record_sent_cote_state("real")
        counts = await market_health.read_sent_cote_states()

    assert client.incr.await_args.args[0].endswith(":real")
    assert counts == {
        "real": 5,
        "pending": 0,
        "no_offer": 3,
        "searching": 2,
        "uncatalogued": 4,
        "unavailable": 1,
    }


@pytest.mark.asyncio
async def test_a_silent_redis_loses_the_measure_not_the_alert():
    """Un Redis muet ne doit jamais faire échouer le chemin d'ingestion."""
    client = MagicMock()
    client.incr = AsyncMock(side_effect=RuntimeError("redis down"))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(market_health, "get_redis_client", lambda: client)
        await market_health.record_sent_cote_state("searching")


def test_the_shipped_threshold_is_seven_days():
    """Le seuil de production, verrouillé par sa valeur.

    Aucun test ne le faisait : les tests d'intégration passaient `days=7` **en
    argument**, si bien que la constante livrée pouvait valoir n'importe quoi sans
    qu'un seul test rougisse. Le défaut a été constaté en la portant à 99 jours —
    toute la suite restait verte.

    C'est ce test-là qui rend la décision vérifiable : il porte sur la constante
    que la production utilise, sans lui substituer de valeur.
    """
    from app.services.market_health import STALE_AFTER_DAYS

    assert STALE_AFTER_DAYS == 7


def test_both_expiry_checks_share_the_same_reference():
    """Les deux contrôles lisent le même seuil, et non deux valeurs qui divergent.

    `notify_stale_current_collections` portait un `2` écrit en dur à côté d'une
    constante qui valait autre chose : le même livre pouvait être périmé pour un
    contrôle et pas pour l'autre. Le défaut n'était visible qu'en lisant les deux
    signatures côte à côte, ce que ce test fait à la place du lecteur.
    """
    import inspect

    from app.services import market_health

    for func in (
        market_health.notify_stale_certified_lows,
        market_health.notify_stale_current_collections,
    ):
        default = inspect.signature(func).parameters["stale_after_days"].default
        assert default == market_health.STALE_AFTER_DAYS, func.__name__


def test_the_reported_delay_matches_the_threshold_that_selected_the_rows():
    """Le délai annoncé dans le message est celui qui a servi à sélectionner.

    Le texte portait « 48 heures » en dur sous un seuil de sept jours : le message
    annonçait un chiffre que le contrôle n'avait pas appliqué.
    """
    from app.services.market_health import StaleCertifiedLow, format_stale_certified_lows

    item = StaleCertifiedLow(
        title="Grimr", isbn13=None, low_cents=545, last_seen=datetime.now(UTC) - timedelta(days=10)
    )
    text = format_stale_certified_lows([item], 1, stale_after_days=7)
    assert "plus de 7 jours" in text
    assert "48 heures" not in text


def test_the_occurrence_token_separates_two_scheduled_runs():
    """Le jeton d'occurrence distingue deux exécutions prévues, et rien d'autre.

    Il est bâti sur la période de la passe (`PRICE_PASS_INTERVAL_MINUTES`) : deux
    exécutions prévues tombent donc toujours dans deux tranches différentes.
    """
    from datetime import UTC, datetime

    from app.tasks.refresh_book_prices import _occurrence_token

    sept = _occurrence_token("refresh_prices_07_00", now=datetime(2026, 10, 16, 7, 0, tzinfo=UTC))
    dixneuf = _occurrence_token(
        "refresh_prices_19_00", now=datetime(2026, 10, 16, 19, 0, tzinfo=UTC)
    )
    assert sept != dixneuf


def test_a_retry_in_the_same_slot_is_the_same_occurrence():
    """Une reprise dans la même tranche vise la même occurrence : un seul jeton."""
    from datetime import UTC, datetime

    from app.tasks.refresh_book_prices import _occurrence_token

    premier = _occurrence_token(
        "refresh_prices_07_00", now=datetime(2026, 10, 16, 7, 0, 5, tzinfo=UTC)
    )
    reprise = _occurrence_token(
        "refresh_prices_07_00", now=datetime(2026, 10, 16, 7, 12, 40, tzinfo=UTC)
    )
    assert premier == reprise


def test_the_next_scheduled_run_is_a_different_occurrence():
    """La tranche suivante est une occurrence distincte : son bilan n'est pas avalé."""
    from datetime import UTC, datetime

    from app.tasks.refresh_book_prices import _occurrence_token

    dixhuit = _occurrence_token(
        "refresh_prices", now=datetime(2026, 10, 16, 7, 15, tzinfo=UTC)
    )
    dixhuit_trente = _occurrence_token(
        "refresh_prices", now=datetime(2026, 10, 16, 7, 45, tzinfo=UTC)
    )
    assert dixhuit != dixhuit_trente


def test_a_delayed_execution_keeps_the_identity_of_what_was_scheduled():
    """Une exécution retardée garde l'identité de son occurrence initiale.

    C'est l'intérêt de dater l'occurrence par l'instant **prévu** : la passe de 19:00
    lancée à 19:31 reste l'occurrence de 19:00, et une reprise de cette même exécution
    prévue retrouve la même clé au lieu d'en inventer une nouvelle.

    Le `now` donné est délibérément dans une **autre tranche** (19:30) : si l'heure
    réelle dat ait daté le jeton, l'assertion tomberait. Sans cette distinction, le test ne
    prouverait rien.
    """
    from datetime import UTC, datetime, timedelta

    from app.tasks.refresh_book_prices import _occurrence_token

    prevu = datetime(2026, 10, 16, 19, 0, tzinfo=UTC)
    premier = _occurrence_token(
        "refresh_prices_19_00",
        scheduled_for=prevu,
        now=datetime(2026, 10, 16, 19, 31, tzinfo=UTC),
    )
    reprise = _occurrence_token(
        "refresh_prices_19_00",
        scheduled_for=prevu,
        now=datetime(2026, 10, 16, 19, 44, tzinfo=UTC),
    )
    assert premier == reprise, "une reprise garde l'occurrence prévue"
    assert "19:00" in premier, "le jeton porte l'instant prévu"
    assert "19:30" not in premier, "l'heure réelle ne doit pas dater l'occurrence"

    # Daté de l'heure réelle, le même instant donnerait un autre jeton.
    depuis_heure_reelle = _occurrence_token(
        "refresh_prices_19_00", now=prevu + timedelta(minutes=31)
    )
    assert depuis_heure_reelle != premier


def test_a_run_crossing_a_boundary_keeps_its_token_until_the_end():
    """Une collecte qui traverse une frontière garde son identité jusqu'au bout.

    Le jeton est calculé **une fois**, au début de la passe, puis voyage avec elle :
    il n'est jamais recalculé à l'heure de la notification. C'est ce qui empêche une
    longue collecte de changer d'occurrence en cours de route.
    """
    from datetime import UTC, datetime

    from app.tasks.refresh_book_prices import _occurrence_token

    debut = datetime(2026, 10, 16, 19, 29, tzinfo=UTC)
    jeton = _occurrence_token("refresh_prices_19_00", scheduled_for=debut)
    # La passe dure plus d'une heure : elle franchit deux frontières de tranche.
    fin = datetime(2026, 10, 16, 20, 31, tzinfo=UTC)
    assert _occurrence_token("refresh_prices_19_00", scheduled_for=debut) == jeton
    # Recalculé à l'heure de fin, le jeton serait différent : c'est ce que le calcul unique évite.
    assert _occurrence_token("refresh_prices_19_00", scheduled_for=fin) != jeton


def test_a_retry_across_a_slot_boundary_is_a_different_occurrence():
    """Deux exécutions prévues de part et d'autre d'une frontière : jetons distincts.

    La période de la passe est de quinze minutes : deux exécutions prévues à :29 et
    :31 sont bien deux passes, et leurs traces ne se confondent pas.
    """
    from datetime import UTC, datetime

    from app.tasks.refresh_book_prices import _occurrence_token

    avant = _occurrence_token(
        "refresh_prices", scheduled_for=datetime(2026, 10, 16, 7, 29, tzinfo=UTC)
    )
    apres = _occurrence_token(
        "refresh_prices", scheduled_for=datetime(2026, 10, 16, 7, 31, tzinfo=UTC)
    )
    assert avant != apres


@pytest.mark.asyncio
async def test_the_seven_day_check_is_computed_from_the_successful_collection_date():
    """Le seuil de sept jours est calculé, et porte sur la collecte réussie.

    Le prix peut rester identique : c'est `last_successful_collection_at` qui
    décide de la fraîcheur, jamais la date du dernier changement de montant.
    """
    finder = AsyncMock(return_value=([], 0))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            market_health.MarketPriceRepository,
            "find_stale_current_collections",
            finder,
        )
        before = datetime.now(UTC)
        await market_health.notify_stale_current_collections(MagicMock())
        after = datetime.now(UTC)

    stale_after = finder.await_args.kwargs["stale_after"]
    assert before - timedelta(days=7, seconds=1) <= stale_after <= after - timedelta(days=7)


SOURCE_KEY = "chasse_aux_livres/current"


def _attempt(status="success", reason=None, http_status=200):
    from app.providers.base import CollectionAttempt

    return CollectionAttempt("chasse_aux_livres", "current", status, reason, http_status)


def test_a_budget_partial_is_a_reading_not_a_source_failure():
    """Mesure du 31 octobre : `incomplete_offers` arrive en HTTP 200, la source répond."""
    assert market_health.source_observation(
        _attempt("partial", "incomplete_offers")
    ) == (market_health.ANSWERED, None)


def test_a_missing_book_is_not_a_source_failure():
    """Un mauvais tome ou une fiche absente accusent le livre, pas la source."""
    for reason in ("volume_mismatch", "identity_mismatch", "empty_response"):
        assert market_health.source_observation(_attempt("failed", reason)) == (
            market_health.ANSWERED,
            None,
        )


def test_a_source_that_does_not_answer_is_a_failure_with_its_cause():
    for reason in ("timeout", "connection", "bot_challenge", "rate_limited"):
        assert market_health.source_observation(_attempt("failed", reason)) == (
            market_health.FAILED,
            reason,
        )
    assert market_health.source_observation(_attempt("failed", "http_status", 429)) == (
        market_health.FAILED,
        "http_status",
    )


def test_a_request_deferred_by_the_governor_says_nothing_about_the_source():
    assert market_health.source_observation(_attempt("skipped", "pass_budget")) is None


def test_one_partial_reading_cannot_clear_an_announced_incident():
    """Un relevé incomplet isolé n'est pas un retour durable : il ne dit rien."""
    run = market_health.SourceRun(market_health.ANSWERED, 1, 0.0, 0.0)
    decisions, announced = market_health.source_decisions({SOURCE_KEY: run}, {SOURCE_KEY})
    assert decisions == {}
    assert announced == {SOURCE_KEY}


def test_an_incident_needs_both_the_failures_and_their_span():
    """Cinq échecs en quinze minutes ne disent rien ; trois sur une heure, si."""
    short = market_health.SourceRun(market_health.FAILED, 5, 0.0, 900.0)
    assert market_health.source_decisions({SOURCE_KEY: short}, set())[0] == {}
    long_enough = market_health.SourceRun(market_health.FAILED, 3, 0.0, 3600.0)
    decisions, announced = market_health.source_decisions({SOURCE_KEY: long_enough}, set())
    assert decisions[SOURCE_KEY][0] == "incident"
    assert announced == {SOURCE_KEY}
