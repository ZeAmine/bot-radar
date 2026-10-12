"""Consommation d'un brouillon dans le processus courant, sans reprise implicite."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey


@dataclass
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_entries: dict[tuple[int, StorageKey], _Entry] = {}


@dataclass
class CreationAttempt:
    """Une invocation ; le commit métier est distinct de la fermeture du FSM."""

    state: FSMContext
    key: tuple[int, StorageKey]
    entry: _Entry
    effect: Literal["not_applied", "unknown", "committed"] = "not_applied"
    released: bool = False

    def release(self) -> None:
        """Libère une seule fois, sans retirer un verrou encore attendu/utilisé."""
        if not self.released:
            self.released = True
            self.entry.lock.release()
            _unregister(self.key, self.entry)

    async def commit_and_close(self, session: Any) -> None:
        """Bloque le rejeu avant commit ; un retour ambigu ne réactive pas le brouillon."""
        await self.state.update_data(creation_outcome="unknown")
        self.effect = "unknown"
        await session.commit()
        self.effect = "committed"
        await self.state.update_data(creation_outcome="committed")
        await self.state.clear()
        self.release()  # Préparation et présentation ne consomment plus le brouillon.


def _unregister(key: tuple[int, StorageKey], entry: _Entry) -> None:
    entry.users -= 1
    if entry.users == 0:
        assert _entries[key] is entry
        del _entries[key]


@asynccontextmanager
async def draft_guard(state: FSMContext) -> AsyncIterator[CreationAttempt]:
    """Sérialise un contexte FSM ; les autres contextes restent indépendants."""
    key = (id(state.storage), state.key)
    entry = _entries.setdefault(key, _Entry())
    entry.users += 1  # Compter aussi les attentes AVANT de céder la main.
    acquired = False
    try:
        await entry.lock.acquire()
        acquired = True
        attempt = CreationAttempt(state, key, entry)
        try:
            yield attempt
        finally:
            attempt.release()
    finally:
        if not acquired:
            _unregister(key, entry)
