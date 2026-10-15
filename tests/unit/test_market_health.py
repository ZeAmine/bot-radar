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


# --- Le bilan ne se répète pas, et ne disparaît pas non plus ---


@pytest.mark.asyncio
async def test_two_scheduled_passes_each_get_their_report():
    """Deux passes planifiées produisent deux bilans, pas un.

    Les deux passes complètes sont à 07:00 et 19:00 — douze heures d'écart. Une
    fenêtre de dédoublonnage de vingt heures, comme celle des autres alertes,
    supprimait donc le bilan de la seconde : le défaut était invisible tant qu'on
    ne comparait pas la fenêtre à l'écart réel entre deux passes.
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

    assert len(sent) == 2, "les deux passes doivent chacune rendre son bilan"


@pytest.mark.asyncio
async def test_the_report_never_waits_on_a_shared_deduplication_window():
    """Le bilan ne dépend d'aucune clé Redis : il ne peut donc pas être avalé.

    C'est la garantie de fond. Tant que le bilan passait par une fenêtre partagée
    avec les alertes critiques, un incident de cotation pouvait le faire taire —
    ou l'inverse. Il est maintenant inconditionnel.
    """
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        class RedisMort:
            def set(self, *_args, **_kwargs):
                raise AssertionError("le bilan ne doit pas consulter Redis")

            def incr(self, *_args, **_kwargs):
                raise AssertionError("le bilan ne doit pas consulter Redis")

        mp.setattr(market_health, "notify_admins", fake_notify)
        mp.setattr(market_health, "get_redis_client", lambda: RedisMort())

        await market_health.notify_incomplete_refresh(
            5, 12, refreshed=4, interrupted=True, incremental=False
        )

    assert len(sent) == 1
