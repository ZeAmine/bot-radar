"""Sérialisation locale des ingestions d'une même annonce dans le processus unique."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from app.core.exceptions import BotRadarError


class IngestionBusyError(BotRadarError):
    """Admission refusée temporairement, sans lecture ni envoi : l'appel peut être rejoué."""

    def __init__(self, reason: str) -> None:
        super().__init__("Ingestion temporairement occupée ; réessayez.")
        self.reason = reason


@dataclass
class _ListingEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    references: int = 0


class ListingIngestionGate:
    """Borne les détenteurs et attenteurs ensemble, sans file d'admission supplémentaire.

    Le poller n'ingère que trois annonces à la fois : 128 participants laissent une
    marge aux appels API tout en bornant les clés et les attenteurs d'une même clé.
    L'attente de 30 s concerne seulement l'acquisition, jamais l'envoi du détenteur.
    Le registre est manipulé sans suspension, sur la seule boucle du processus.
    """

    def __init__(self, max_participants: int = 128, wait_timeout: float = 30.0) -> None:
        self.max_participants = max_participants
        self.wait_timeout = wait_timeout
        self._entries: dict[tuple[str, str], _ListingEntry] = {}
        self._participants = 0

    @asynccontextmanager
    async def protect(self, source_name: str, external_id: str) -> AsyncIterator[None]:
        """Garde la clé jusqu'à la sortie, et nettoie aussi les attentes annulées ou expirées."""
        if self._participants >= self.max_participants:
            raise IngestionBusyError("saturated")
        key = (source_name, external_id)
        entry = self._entries.setdefault(key, _ListingEntry())
        entry.references += 1
        self._participants += 1
        acquired = False
        try:
            try:
                async with asyncio.timeout(self.wait_timeout):
                    await entry.lock.acquire()
                acquired = True
            except TimeoutError as exc:
                raise IngestionBusyError("timeout") from exc
            yield
        finally:
            if acquired:
                entry.lock.release()
            entry.references -= 1
            self._participants -= 1
            if entry.references == 0:
                del self._entries[key]


# Partagé par toutes les instances du use case, API et poller compris.
listing_ingestion_gate = ListingIngestionGate()
