import asyncio

import pytest

from app.providers.cal_governor import CalGovernor, CalSuspendedError
from app.services import market_health


class RecoverableStore:
    unavailable = True

    async def eval(self, *_args):
        if self.unavailable:
            raise ConnectionError("redis://synthetic-secret@example.invalid")
        return ["0", "", "", "0", ""]


async def test_a_successful_redis_operation_clears_unavailability(caplog):
    store = RecoverableStore()
    governor = CalGovernor(store=store)
    with pytest.raises(CalSuspendedError):
        await governor.admit_book("test")
    assert governor.storage_unavailable
    store.unavailable = False
    await governor.snapshot()
    assert governor.storage_unavailable is False
    assert "synthetic-secret" not in caplog.text


async def test_storage_health_alerts_once_and_rearms_after_recovery():
    health_type = getattr(market_health, "CalStorageHealth", None)
    assert health_type is not None, "La surveillance persistante Redis manque"
    now = [0.0]
    store = RecoverableStore()
    governor = CalGovernor(store=store, monotonic_clock=lambda: now[0])
    messages = []

    async def notify(message):
        messages.append(message)
        return "accepted"

    health = health_type(governor=governor, notify=notify)
    await health.check()
    now[0] = 299
    await health.check()
    assert messages == []
    now[0] = 300
    await asyncio.gather(*(health.check() for _ in range(5)))
    assert len(messages) == 1
    now[0] = 1000
    await health.check()
    assert len(messages) == 1
    store.unavailable = False
    await health.check()
    assert governor.storage_unavailable is False
    store.unavailable = True
    await health.check()
    now[0] = 1300
    await health.check()
    assert len(messages) == 2


@pytest.mark.parametrize(("outcome", "expected"), [("failed", 3), ("uncertain", 1)])
async def test_storage_notification_attempts_are_bounded(outcome, expected):
    health_type = getattr(market_health, "CalStorageHealth", None)
    assert health_type is not None, "La surveillance persistante Redis manque"
    now = [0.0]
    governor = CalGovernor(store=RecoverableStore(), monotonic_clock=lambda: now[0])
    messages = []

    async def notify(message):
        messages.append(message)
        return outcome

    health = health_type(governor=governor, notify=notify)
    for value in (0, 299, 300, 301, 599, 600, 899, 900, 1200):
        now[0] = value
        await health.check()
        if value == 299:
            assert messages == []
        if value in (300, 301, 599):
            assert len(messages) == 1
    assert len(messages) == expected


async def test_a_success_between_failures_restarts_the_persistence_window():
    now = [0.0]
    store = RecoverableStore()
    governor = CalGovernor(store=store, monotonic_clock=lambda: now[0])
    messages = []

    async def notify(message):
        messages.append(message)
        return "accepted"

    health = market_health.CalStorageHealth(governor=governor, notify=notify)
    await health.check()
    now[0] = 250
    store.unavailable = False
    await governor.snapshot()
    store.unavailable = True
    now[0] = 251
    await health.check()
    now[0] = 550
    await health.check()
    assert messages == []
    now[0] = 551
    await health.check()
    assert len(messages) == 1


async def test_notification_exception_is_uncertain_without_repeated_attempts(caplog):
    now = [0.0]
    governor = CalGovernor(store=RecoverableStore(), monotonic_clock=lambda: now[0])
    attempts = []

    async def notify(_message):
        attempts.append(True)
        raise ConnectionError("synthetic-secret")

    health = market_health.CalStorageHealth(governor=governor, notify=notify)
    await health.check()
    now[0] = 300
    await health.check()
    now[0] = 900
    await health.check()
    assert attempts == [True]
    assert "incertain" in caplog.text
    assert "synthetic-secret" not in caplog.text
