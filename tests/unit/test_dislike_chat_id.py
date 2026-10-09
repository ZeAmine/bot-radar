"""L'identifiant Telegram dépasse int32 : la recherche ne caste pas en INTEGER.

asyncpg refuse de lier un entier plus grand que 2_147_483_647 sur un paramètre
INTEGER. Le bouton « Confirmer » d'un faux positif tombait là, avant toute
réponse Telegram. Ce test compile la requête telle que le dépôt l'envoie, pour
le dialecte PostgreSQL, sans base : SQLite n'a pas cette limite.
"""

import uuid
from typing import Any

from sqlalchemy.dialects import postgresql

from app.db.repositories.autotune_repo import record_listing_dislike

TELEGRAM_CHAT_ID = 6_429_948_805


class _Capture:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        return _Empty()

    def add(self, _event: object) -> None:
        return None


class _Empty:
    def scalar_one_or_none(self) -> None:
        return None


async def _lookup(chat_id: int | None) -> Any:
    session = _Capture()
    await record_listing_dislike(
        session,
        listing_id=uuid.uuid4(),
        alert_id=None,
        chat_id=chat_id,
    )
    return session.statements[1].compile(dialect=postgresql.dialect())


async def test_large_chat_id_lookup_is_not_cast_to_integer() -> None:
    compiled = await _lookup(TELEGRAM_CHAT_ID)
    sql = str(compiled)
    assert "AS INTEGER" not in sql
    assert "AS VARCHAR" in sql
    assert compiled.params["metadata_1"] == "chat_id"
    assert compiled.params["param_1"] == str(TELEGRAM_CHAT_ID)


async def test_missing_chat_id_lookup_stays_null_without_integer_cast() -> None:
    compiled = await _lookup(None)
    sql = str(compiled)
    assert "AS INTEGER" not in sql
    assert "IS NULL" in sql
    assert compiled.params["metadata_1"] == "chat_id"
