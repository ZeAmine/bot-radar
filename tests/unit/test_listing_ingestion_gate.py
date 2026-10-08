"""Capacité, annulation et durée d'attente du registre local dédié à l'ingestion."""

import asyncio

import pytest

from app.use_cases.listing_ingestion_gate import IngestionBusyError, ListingIngestionGate


async def test_cancelled_waiter_preserves_holder_and_next_waiter():
    gate = ListingIngestionGate()
    started, entered, release = (asyncio.Event() for _ in range(3))

    async def participant(signal):
        signal.set()
        async with gate.protect("vinted", "1"):
            entered.set()
            await release.wait()

    async with gate.protect("vinted", "1"):
        cancelled = asyncio.create_task(participant(started))
        await started.wait()
        started.clear()
        next_waiter = asyncio.create_task(participant(started))
        await started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert gate._participants == 2
        assert gate._entries[("vinted", "1")].references == 2
        assert not entered.is_set()
    await entered.wait()
    assert gate._participants == 1
    release.set()
    await next_waiter
    assert not gate._entries
    assert gate._participants == 0


async def test_capacity_counts_waiters_and_refuses_without_an_admission_queue():
    gate = ListingIngestionGate(max_participants=2)
    started = asyncio.Event()

    async def waiter():
        started.set()
        async with gate.protect("vinted", "1"):
            pass

    async with gate.protect("vinted", "1"):
        task = asyncio.create_task(waiter())
        await started.wait()
        for external_id in ("1", "2"):
            with pytest.raises(IngestionBusyError) as error:
                async with gate.protect("vinted", external_id):
                    pytest.fail("Une admission saturée a été acceptée")
            assert error.value.reason == "saturated"
            assert gate._participants == 2
            assert len(gate._entries) == 1
    await task
    assert not gate._entries
    # La capacité est réutilisable, y compris pour une autre clé.
    async with gate.protect("vinted", "2"):
        assert gate._participants == 1
    assert gate._participants == 0


async def test_timeout_removes_only_the_waiter_and_never_interrupts_holder():
    gate = ListingIngestionGate(wait_timeout=0)
    async with gate.protect("vinted", "1"):
        with pytest.raises(IngestionBusyError) as error:
            async with gate.protect("vinted", "1"):
                pytest.fail("Une acquisition expirée a été acceptée")
        assert error.value.reason == "timeout"
        assert gate._participants == 1
        assert gate._entries[("vinted", "1")].lock.locked()
    assert not gate._entries


async def test_exception_and_successive_keys_release_every_entry():
    gate = ListingIngestionGate(max_participants=1)
    for index in range(256):
        with pytest.raises(RuntimeError, match="Erreur factice"):
            async with gate.protect("vinted", str(index)):
                raise RuntimeError("Erreur factice")
        assert not gate._entries
        assert gate._participants == 0


async def test_source_is_part_of_identity():
    gate = ListingIngestionGate(max_participants=2)
    async with gate.protect("vinted", "1"), gate.protect("other", "1"):
        assert len(gate._entries) == 2
    assert not gate._entries
