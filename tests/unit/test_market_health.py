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


@pytest.mark.asyncio
async def test_notification_is_sent_once_per_window():
    """Le job passe deux fois par jour : le signal ne doit pas se répéter."""
    repo_result = AsyncMock(return_value=[("Dead Train", "9782380463972", 1357, None)])

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
            AsyncMock(return_value=[]),
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
            AsyncMock(return_value=[("Dead Train", "9782380463972", 1357, None)]),
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
