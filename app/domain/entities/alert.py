"""Entité représentant une alerte utilisateur."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID


@dataclass(slots=True)
class PauseActionProgress:
    """Connaissance locale d'une invocation : un commit échoué ne prouve pas son absence."""

    effect: Literal["not_started", "not_applied", "unknown", "committed"] = "not_started"
    token: UUID | None = None
    user_id: UUID | None = None
    update_id: int = 0
    callback_id: str = ""
    result: str = ""
    current_status: str = ""
    stage: Literal[
        "dispatch", "lookup", "mutation", "commit", "confirmation", "card", "handler_returned"
    ] = "dispatch"
