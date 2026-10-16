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
    # Jamais observé est plus grave qu'anciennement observé : les deux se disent.
    assert "jamais observée" in message
    assert "3,90 €" not in message
    # Le message explique pourquoi le chiffre reste là.
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
    # La réservation doit être un vrai NX court, sinon le dédoublonnage ne tient pas.
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


# --- Source de cotation muette depuis 48 h ---


@pytest.mark.asyncio
async def test_a_silent_provider_is_reported_once_per_window():
    """Une source qui cesse de répondre ne se signale pas d'elle-même."""
    now = datetime.now(UTC)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            market_health.MarketPriceRepository,
            "find_silent_providers",
            AsyncMock(return_value=[("momox", now - timedelta(hours=50))]),
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
    assert "48 h" in sent[0]
    # L'âge de la dernière réponse se dit en heures : c'est ce qui a changé.
    assert "50 h" in sent[0]


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


# --- Alertes de fin de passe ---


@pytest.mark.asyncio
async def test_an_incomplete_pass_is_reported():
    """Une passe tronquée par le plafond de durée doit se voir, pas se taire.

    Elle est décrite comme une **information** : le plafond de durée est le
    fonctionnement normal d'un lot qui ne tient pas dans sa fenêtre, pas une panne.
    """
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        await market_health.notify_incomplete_refresh(
            5, 12, refreshed=4, interrupted=True, incremental=False
        )

    assert len(sent) == 1
    text = sent[0]
    assert "5/12" in text
    # Les quatre nombres sont distincts et nommés : traités, mis à jour, restants.
    assert "Traités : 5/12 livres" in text
    assert "Mis à jour : 4 livres" in text
    assert "Restants à traiter : 7 livres" in text


@pytest.mark.asyncio
async def test_an_incomplete_pass_carries_no_warning_marker():
    """Une passe coupée par le plafond n'est pas un avertissement.

    Elle l'était : le message portait un ⚠️ et le journal un `warning`, pour un
    fonctionnement prévu. Le bruit rendait invisible ce qui, lui, est anormal.
    """
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        await market_health.notify_incomplete_refresh(
            5, 12, refreshed=4, interrupted=True, incremental=True, batch_size=8
        )

    assert "⚠️" not in sent[0]
    assert "incrémental" in sent[0]


@pytest.mark.asyncio
async def test_a_full_pass_reports_when_the_next_full_pass_is_known():
    """La date annoncée est celle d'une passe **complète** confirmée par le planificateur."""
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)
        mp.setattr(
            market_health, "next_full_pass_note",
            lambda: "\nProchaine passe complète prévue : 01/01 à 19:00.",
        )

        await market_health.notify_incomplete_refresh(
            5, 12, refreshed=4, interrupted=True, incremental=False
        )

    assert "Prochaine passe complète prévue" in sent[0]


@pytest.mark.asyncio
async def test_an_incremental_pass_never_promises_a_date():
    """Un lot incrémental ne peut pas absorber l'arriéré : il ne promet aucune reprise.

    Un passage glissant traite huit livres ; annoncer une date laisserait croire que
    les restants sont pris en charge, alors que rien ne le garantit.
    """
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)
        mp.setattr(
            market_health, "next_full_pass_note",
            lambda: "\nProchaine passe complète prévue : 01/01 à 19:00.",
        )

        await market_health.notify_incomplete_refresh(
            2, 8, refreshed=1, interrupted=True, incremental=True, batch_size=8
        )

    assert "Prochaine passe complète prévue" not in sent[0]
    assert "ordre de la file" in sent[0]


def test_the_next_full_pass_note_is_empty_without_a_scheduler():
    """Sans planificateur pour la confirmer, la date n'est pas inventée.

    C'est la règle qui rend la phrase honnête : une date qui ne vient pas du
    planificateur serait une promesse que rien ne tient, et le message se tairait
    plutôt que de la produire.
    """
    import app.tasks.scheduler as scheduler_module

    class Exploding:
        def get_jobs(self):
            raise RuntimeError("pas de planificateur")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scheduler_module, "scheduler", Exploding(), raising=False)
        assert market_health.next_full_pass_note() == ""


@pytest.mark.asyncio
async def test_a_complete_pass_says_nothing():
    """Le succès n'est pas une notification."""
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        await market_health.notify_incomplete_refresh(
            12, 12, refreshed=12, interrupted=False, incremental=False
        )
        await market_health.notify_no_prices_found(
            0, 0, 0, incremental=False, mode_label="complet (0 livres)"
        )
        await market_health.notify_no_prices_found(
            12, 12, 3, incremental=False, mode_label="complet (12 livres)"
        )
        await market_health.notify_high_error_ratio(12, 2, mode_label="complet (12 livres)")

    assert sent == []


@pytest.mark.asyncio
async def test_a_pass_without_any_price_is_reported():
    """Trois semaines de cotes muettes se sont passées sans qu'aucun signal ne parte."""
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        client = MagicMock()
        client.set = AsyncMock(return_value=True)
        mp.setattr(market_health, "get_redis_client", lambda: client)

        await market_health.notify_no_prices_found(
            10, 10, 0, incremental=False, mode_label="complet (10 livres)", errors=10
        )

    assert len(sent) == 1
    assert "0 cote" in sent[0]


@pytest.mark.asyncio
async def test_un_lot_incremental_sans_cote_ne_signale_rien():
    """Un lot de 8 livres sans cote n'est pas une panne : le lot est trié vers l'échec.

    C'est l'alerte qui revenait jusqu'à quarante-huit fois par jour. Le job
    glissant traite huit livres toutes les trente minutes, et sa sélection
    ordonne par `checked_at ASC NULLS FIRST` : un livre sans cote revient en tête
    de file après six heures là où un livre coté attend vingt-quatre, donc les
    livres sans offre y sont structurellement sur-représentés. « 0 cote sur 8 »
    ne prouve rien, et l'affirmer coûtait un message par demi-heure.
    """
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        client = MagicMock()
        client.set = AsyncMock(return_value=True)
        mp.setattr(market_health, "get_redis_client", lambda: client)

        await market_health.notify_no_prices_found(
            8, 8, 0, incremental=True, mode_label="incrémental (8 livres)"
        )

    assert sent == [], "un lot partiel ne peut pas conclure à une panne"
    # Rien n'est même réservé : on n'a pas consommé la fenêtre d'une future alerte.
    assert client.set.await_count == 0


@pytest.mark.asyncio
async def test_la_passe_sans_cote_ne_signale_quune_seule_fois():
    """Le dédoublonnage : la fenêtre Redis vaut comme pour les deux autres alertes."""
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        client = MagicMock()
        client.set = AsyncMock(side_effect=[True, False])
        mp.setattr(market_health, "get_redis_client", lambda: client)

        await market_health.notify_no_prices_found(
            10, 10, 0, incremental=False, mode_label="complet (10 livres)", errors=10
        )
        await market_health.notify_no_prices_found(
            10, 10, 0, incremental=False, mode_label="complet (10 livres)", errors=10
        )

    assert len(sent) == 1
    assert client.set.await_args.kwargs["nx"] is True


@pytest.mark.asyncio
async def test_le_message_ne_saccuse_plus_les_sources():
    """« Toutes les sources sont muettes » était faux : c'était un fait par livre.

    La base compte des cotes ; déduire de là que les sources sont muettes
    accusait quatre fournisseurs sur une observation portant sur quelques
    livres. Le message nomme ce qui a été observé et renvoie à l'alerte qui, elle,
    sait nommer une source.
    """
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        client = MagicMock()
        client.set = AsyncMock(return_value=True)
        mp.setattr(market_health, "get_redis_client", lambda: client)

        await market_health.notify_no_prices_found(
            10, 10, 0, incremental=False, mode_label="complet (10 livres)", errors=10
        )

    assert "Toutes les sources sont muettes" not in sent[0]
    assert "Aucune source" not in sent[0]
    assert "en erreur" in sent[0]


@pytest.mark.asyncio
async def test_a_high_error_ratio_is_reported_with_a_meaningful_minimum():
    """Un ratio sur deux livres n'a aucun sens : trois erreurs au minimum."""
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        client = MagicMock()
        client.set = AsyncMock(return_value=True)
        mp.setattr(market_health, "get_redis_client", lambda: client)

        # 1 erreur sur 2 livres : ratio élevé mais nombre trop faible.
        await market_health.notify_high_error_ratio(2, 1, mode_label="complet (2 livres)")
        # 4 erreurs sur 6 livres : au-dessus des deux seuils.
        await market_health.notify_high_error_ratio(6, 4, mode_label="complet (6 livres)")

    assert len(sent) == 1
    assert "4/6" in sent[0]


@pytest.mark.asyncio
async def test_high_error_ratio_is_sent_once_and_names_the_providers():
    """La même panne ne se répète pas, et le message dit quelle source a échoué.

    Le job glissant passe toutes les trente minutes. Sans fenêtre, cinq livres
    dont un fournisseur ne répond pas produisaient un message à chaque passe.
    """
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        client = MagicMock()
        client.set = AsyncMock(side_effect=[True, False])
        mp.setattr(market_health, "get_redis_client", lambda: client)

        kwargs = {
            "mode_label": "incrémental (8 livres)",
            "failed_providers": ("chasse_aux_livres",),
            "degraded": 5,
        }
        await market_health.notify_high_error_ratio(8, 0, **kwargs)
        await market_health.notify_high_error_ratio(8, 0, **kwargs)

    assert len(sent) == 1
    assert "chasse_aux_livres" in sent[0]
    assert "conservés" in sent[0]
    assert "n'ont pas été mises à jour" not in sent[0]
    assert client.set.await_args_list[0].kwargs["nx"] is True


# --- Diagnostic : avec quelle cote une alerte BD part-elle ? ---


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

    assert classify_sent_cote_state("<b>Prix bas constaté :</b> 19,98 €") == "real"
    assert classify_sent_cote_state(PENDING_COTE_LINE) == "pending"
    assert classify_sent_cote_state(SEARCHING_BOOK_LINE) == "searching"
    # La ligne d'absence est datée : elle se reconnaît à sa phrase, pas à son tout.
    assert classify_sent_cote_state(f"{NO_OFFER_LINE} (relevé du 06/10 à 11:30).") == "no_offer"
    # Aucune source ne catalogue l'ouvrage : ce n'est pas une recherche en cours,
    # et surtout pas une cote chiffrée qu'on aurait mesurée.
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


# --- Le seuil réellement livré ---


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


# --- Un bilan au plus par occurrence planifiée ---


def _fenetres_ouvertes(mp):
    """Redis qui accorde chaque clé une seule fois, comme le vrai `SET NX`."""
    vues: set[str] = set()

    class Redis:
        async def set(self, key, _value, **options):
            if options.get("nx") and key in vues:
                return None
            vues.add(key)
            return True

    mp.setattr(market_health, "get_redis_client", lambda: Redis())
    return vues


@pytest.mark.asyncio
async def test_two_attempts_for_the_same_occurrence_produce_one_report():
    """Deux tentatives pour la **même** occurrence planifiée : un seul bilan.

    C'est la garantie demandée. Une reprise après erreur, ou un doublon
    d'ordonnancement, retente la même exécution prévue : elle ne doit pas
    redoubler le message.
    """
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)
        _fenetres_ouvertes(mp)

        for _ in range(2):
            await market_health.notify_incomplete_refresh(
                5, 12, refreshed=4, interrupted=True, incremental=False,
                occurrence="refresh_prices_07_00@2026-10-16T07:00:00+00:00",
            )

    assert len(sent) == 1, "la même occurrence ne produit qu'un bilan"


@pytest.mark.asyncio
async def test_two_distinct_occurrences_twelve_hours_apart_produce_two_reports():
    """Deux occurrences distinctes séparées de douze heures : un bilan chacune.

    C'est l'autre moitié de la garantie, et celle qu'une fenêtre de vingt heures
    cassait : les passes de 07:00 et 19:00 sont à douze heures d'écart, donc la
    seconde tombait dans la fenêtre de la première et disparaissait.
    """
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)
        _fenetres_ouvertes(mp)

        await market_health.notify_incomplete_refresh(
            5, 12, refreshed=4, interrupted=True, incremental=False,
            occurrence="refresh_prices_07_00@2026-10-16T07:00:00+00:00",
        )
        await market_health.notify_incomplete_refresh(
            5, 12, refreshed=4, interrupted=True, incremental=False,
            occurrence="refresh_prices_19_00@2026-10-16T19:00:00+00:00",
        )

    assert len(sent) == 2, "chaque occurrence rend son propre bilan"


@pytest.mark.asyncio
async def test_the_deduplication_of_one_occurrence_does_not_silence_the_next():
    """La clé d'une occurrence n'étouffe jamais celle de la suivante.

    Le point précis : le dédoublonnage borne une **exécution**, pas une durée. Une
    clé d'occurrence ne peut donc pas avaler l'occurrence d'après, quel que soit
    l'écart entre les deux.
    """
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)
        cles = _fenetres_ouvertes(mp)

        # Même job, deux jours différents : deux occurrences, donc deux clés.
        for jour in ("16", "17"):
            await market_health.notify_incomplete_refresh(
                5, 12, refreshed=4, interrupted=True, incremental=False,
                occurrence=f"refresh_prices_07_00@2026-10-{jour}T07:00:00+00:00",
            )

    assert len(cles) == 2, "le dédoublonnage borne une exécution, jamais la suivante"


@pytest.mark.asyncio
async def test_a_report_without_an_occurrence_is_never_silenced():
    """Hors planificateur, le bilan part sans dédoublonnage.

    Mieux vaut un doublon qu'un incident passé sous silence : sans identité
    d'occurrence, il n'y a rien à dédoublonner honnêtement.
    """
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        for _ in range(2):
            await market_health.notify_incomplete_refresh(
                5, 12, refreshed=4, interrupted=True, incremental=False
            )

    assert len(sent) == 2


def test_the_occurrence_token_separates_two_scheduled_runs():
    """Le jeton d'occurrence distingue deux exécutions prévues, et rien d'autre.

    Il est bâti sur la tranche de trente minutes : la plus petite période planifiée
    du rafraîchissement est la demi-heure (le lot glissant passe à :15 et :45), donc
    deux exécutions prévues tombent toujours dans deux tranches différentes.
    """
    from datetime import UTC, datetime

    from app.tasks.refresh_book_prices import _occurrence_token

    sept = _occurrence_token("refresh_prices_07_00", now=datetime(2026, 10, 16, 7, 0, tzinfo=UTC))
    dixneuf = _occurrence_token(
        "refresh_prices_19_00", now=datetime(2026, 10, 16, 19, 0, tzinfo=UTC)
    )
    assert sept != dixneuf


def test_a_retry_in_the_same_half_hour_is_the_same_occurrence():
    """Une reprise dans la même demi-heure vise la même occurrence : un seul bilan."""
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
        "refresh_prices_incremental", now=datetime(2026, 10, 16, 7, 15, tzinfo=UTC)
    )
    dixhuit_trente = _occurrence_token(
        "refresh_prices_incremental", now=datetime(2026, 10, 16, 7, 45, tzinfo=UTC)
    )
    assert dixhuit != dixhuit_trente


# --- La borne du dédoublonnage : le TTL, et lui seul, décide ---


class _RedisQuiExpire:
    """Redis minimal qui honore vraiment `ex` : la clé redevient libre après le délai.

    Il faut un Redis fidèle plutôt qu'un ensemble de clés, parce que c'est
    précisément l'expiration qui est testée.
    """

    def __init__(self, horloge) -> None:
        self._possedees: dict[str, float] = {}
        self._horloge = horloge

    async def set(self, key, _value, **options):
        if self._possedees.get(key, 0) > self._horloge():
            return None
        delai = options.get("ex")
        self._possedees[key] = self._horloge() + (delai if delai else 0)
        return True


def _horloge_et_redis(mp, market_health):
    """Branche une horloge virtuelle unique sur les deux gardes de `_claim_slot`.

    `_claim_slot` dédoublonne deux fois : en mémoire, puis dans Redis. Les deux
    lisent `monotonic`, et le repli mémoire est vidé à chaque appel pour que le test
    porte bien sur l'expiration de la clé Redis — sans quoi le garde-fou local
    masquerait le TTL et le test passerait quelle que soit la durée.

    L'instance Redis est **unique** pour tout le test : une nouvelle instance à chaque
    appel oublierait les clés posées, et le test passerait sans rien démontrer.
    """
    etat = {"t": 0.0}

    def horloge() -> float:
        return etat["t"]

    faux_redis = _RedisQuiExpire(horloge)
    mp.setattr(market_health, "monotonic", horloge)
    mp.setattr(market_health, "get_redis_client", lambda: faux_redis)
    return etat


@pytest.mark.asyncio
async def test_two_attempts_beyond_the_ttl_produce_a_second_report():
    """Deux tentatives de la même occurrence au-delà du TTL : deux bilans.

    C'est la **borne**, et elle doit être énoncée plutôt que niée. La clé a expiré,
    donc la tentative suivante retrouve une clé libre et le bilan repart. Le test fige
    ce comportement réel : la garantie n'est pas éternelle.
    """
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)
        etat = _horloge_et_redis(mp, market_health)

        occurrence = "refresh_prices_07_00@2026-10-16T07:00:00+00:00"
        for _ in range(2):
            # Le repli mémoire est vidé : seul le TTL Redis peut dédoublonner ici.
            mp.setattr(market_health, "_notification_deadlines", {})
            await market_health.notify_incomplete_refresh(
                5, 12, refreshed=4, interrupted=True, incremental=False, occurrence=occurrence,
            )
            etat["t"] = market_health.INCOMPLETE_NOTIFY_TTL_SECONDS + 60.0

    assert len(sent) == 2, "au-delà du TTL la clé a expiré : la garantie est bornée"


@pytest.mark.asyncio
async def test_two_attempts_inside_the_ttl_produce_one_report():
    """Deux tentatives de la même occurrence dans le TTL : un seul bilan.

    L'autre face de la borne, et la preuve que le test précédent mesure bien le TTL :
    à une seconde près sous la limite, le second bilan est absorbé.
    """
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)
        etat = _horloge_et_redis(mp, market_health)

        occurrence = "refresh_prices_07_00@2026-10-16T07:00:00+00:00"
        for _ in range(2):
            mp.setattr(market_health, "_notification_deadlines", {})
            await market_health.notify_incomplete_refresh(
                5, 12, refreshed=4, interrupted=True, incremental=False, occurrence=occurrence,
            )
            etat["t"] = market_health.INCOMPLETE_NOTIFY_TTL_SECONDS - 1.0

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_the_bound_sits_where_the_constant_says_it_does():
    """La borne est exactement celle de la constante : ni plus tôt, ni plus tard.

    Un décalage d'une seconde de part et d'autre de `INCOMPLETE_NOTIFY_TTL_SECONDS`
    suffit à changer le résultat. C'est ce qui interdit de présenter la garantie
    comme absolue, et ce qui fixe la durée de protection sur une valeur vérifiable.
    """
    resultats = []
    for delta in (-1.0, +1.0):
        with pytest.MonkeyPatch.context() as mp:
            sent: list[str] = []

            async def fake_notify(text: str, _recu: list[str] = sent) -> None:
                _recu.append(text)

            mp.setattr(market_health, "notify_admins", fake_notify)
            etat = _horloge_et_redis(mp, market_health)

            occurrence = "refresh_prices_19_00@2026-10-16T19:00:00+00:00"
            for _ in range(2):
                mp.setattr(market_health, "_notification_deadlines", {})
                await market_health.notify_incomplete_refresh(
                    5, 12, refreshed=4, interrupted=True, incremental=False,
                    occurrence=occurrence,
                )
                etat["t"] = market_health.INCOMPLETE_NOTIFY_TTL_SECONDS + delta
            resultats.append(len(sent))

    assert resultats == [1, 2], f"une seconde de part et d'autre change la borne : {resultats}"


def test_the_protection_window_is_the_one_that_ships():
    """La durée de protection est verrouillée par sa valeur, pas seulement par sa forme.

    Les tests ci-dessus déplacent l'horloge autour de la constante : ils vérifient le
    mécanisme, mais passeraient quelle que soit la durée. Celui-ci fixe la valeur
    réellement expédiée, pour que la borne annoncée ne puisse pas dériver en silence.

    Quinze minutes couvrent la fenêtre de réexécution prise en charge — une reprise
    d'erreur, un doublon d'ordonnancement —, et restent très en deçà de l'écart entre
    deux occurrences (trente minutes pour le lot glissant, douze heures entre les
    deux passes complètes). La borne ne peut donc jamais avaler l'occurrence suivante.
    """
    assert market_health.INCOMPLETE_NOTIFY_TTL_SECONDS == 15 * 60


def test_the_bound_stays_below_the_gap_between_two_occurrences():
    """La borne doit rester strictement sous l'écart minimum entre deux occurrences.

    C'est la propriété qui protège le bilan de l'occurrence suivante — et celle que la
    fenêtre de vingt heures violait, les deux passes complètes n'étant séparées que de
    douze heures. Vérifiée sur la plus petite période planifiée : trente minutes.
    """
    plus_petit_ecart = 30 * 60
    assert plus_petit_ecart > market_health.INCOMPLETE_NOTIFY_TTL_SECONDS, (
        "une borne au-delà de l'écart entre deux occurrences supprimerait un bilan"
    )


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

    # Et la preuve que les deux sources sont distinctes : daté de l'heure réelle, le
    # même instant donnerait un autre jeton, dans la tranche de 19:30.
    depuis_heure_reelle = _occurrence_token(
        "refresh_prices_19_00", now=prevu + timedelta(minutes=31)
    )
    assert depuis_heure_reelle != premier


def test_a_run_crossing_a_boundary_keeps_its_token_until_notification():
    """Une collecte qui traverse une frontière garde son identité jusqu'au bilan.

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
    # Recalculé à l'heure de fin, le jeton serait différent — c'est précisément ce que
    # le calcul unique au début de la passe évite.
    assert _occurrence_token("refresh_prices_19_00", scheduled_for=fin) != jeton


def test_a_retry_across_a_slot_boundary_is_a_different_occurrence():
    """Deux exécutions prévues de part et d'autre d'une frontière : jetons distincts.

    Trente minutes est la période du lot glissant : deux exécutions prévues à :29 et
    :35 sont bien deux occurrences, et chacune rend son bilan.
    """
    from datetime import UTC, datetime

    from app.tasks.refresh_book_prices import _occurrence_token

    avant = _occurrence_token(
        "refresh_prices_incremental", scheduled_for=datetime(2026, 10, 16, 7, 29, tzinfo=UTC)
    )
    apres = _occurrence_token(
        "refresh_prices_incremental", scheduled_for=datetime(2026, 10, 16, 7, 31, tzinfo=UTC)
    )
    assert avant != apres
