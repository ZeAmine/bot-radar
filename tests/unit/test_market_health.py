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
    assert "13,57 €" in message
    assert "rien depuis 23 j" in message
    # Jamais observé est plus grave qu'anciennement observé : les deux se disent.
    assert "jamais observée" in message
    assert "3,90 €" in message
    # Le message explique pourquoi le chiffre reste là.
    assert "ne se périment pas toutes seules" in message


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
    """Une passe tronquée par le plafond de durée doit se voir, pas se taire."""
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        await market_health.notify_incomplete_refresh(5, 12, mode_label="complet (12 livres)")

    assert len(sent) == 1
    assert "5/12" in sent[0]


@pytest.mark.asyncio
async def test_a_complete_pass_says_nothing():
    """Le succès n'est pas une notification."""
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        await market_health.notify_incomplete_refresh(12, 12, mode_label="complet (12 livres)")
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
            10, 10, 0, incremental=False, mode_label="complet (10 livres)"
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
            10, 10, 0, incremental=False, mode_label="complet (10 livres)"
        )
        await market_health.notify_no_prices_found(
            10, 10, 0, incremental=False, mode_label="complet (10 livres)"
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
            10, 10, 0, incremental=False, mode_label="complet (10 livres)"
        )

    assert "Toutes les sources sont muettes" not in sent[0]
    assert "Aucune source" not in sent[0]


@pytest.mark.asyncio
async def test_a_high_error_ratio_is_reported_with_a_meaningful_minimum():
    """Un ratio sur deux livres n'a aucun sens : trois erreurs au minimum."""
    with pytest.MonkeyPatch.context() as mp:
        sent: list[str] = []

        async def fake_notify(text: str) -> None:
            sent.append(text)

        mp.setattr(market_health, "notify_admins", fake_notify)

        # 1 erreur sur 2 livres : ratio élevé mais nombre trop faible.
        await market_health.notify_high_error_ratio(2, 1, mode_label="complet (2 livres)")
        # 4 erreurs sur 6 livres : au-dessus des deux seuils.
        await market_health.notify_high_error_ratio(6, 4, mode_label="complet (6 livres)")

    assert len(sent) == 1
    assert "4/6" in sent[0]
