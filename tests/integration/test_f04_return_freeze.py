"""Le gel de retour est éprouvé sur PostgreSQL et ne touche pas les anciennes pauses."""

import os

import pytest
from sqlalchemy import select, text

from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.user import User
from app.domain.entities.alert_preparation import PreparationRequest
from app.domain.enums import AlertStatus
from scripts import freeze_bd_preparations as tool
from tests.database import postgres_replayed_database

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL requis pour le gel JSONB du retour",
)


async def test_freeze_preserves_context_legacy_rows_and_is_idempotent(monkeypatch):
    async with postgres_replayed_database() as db:
        monkeypatch.setattr(tool, "AsyncSessionLocal", db.maker)
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        await db.session.execute(text("DELETE FROM alembic_version"))
        await db.session.execute(
            text("INSERT INTO alembic_version VALUES ('0010_alert_preparation')")
        )
        category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        user = User(telegram_user_id=601, is_allowed=True)
        db.session.add_all([category, user])
        await db.session.flush()
        request = PreparationRequest(
            author_hint="Auteur demandé", state="blocked", error_code="catalog_write"
        ).stored()
        marked = Alert(
            user_id=user.id,
            category_id=category.id,
            name="Marquée",
            include_keywords=[],
            status=AlertStatus.PAUSED,
            preparation=request,
        )
        legacy = Alert(
            user_id=user.id,
            category_id=category.id,
            name="Ancienne",
            include_keywords=[],
            status=AlertStatus.PAUSED,
        )
        db.session.add_all([marked, legacy])
        await db.session.commit()
        dry = await tool.freeze(execute=False)
        assert dry["requests_to_freeze"] == 1
        await db.session.refresh(marked)
        assert marked.preparation == request
        result = await tool.freeze(execute=True)
        assert result["requests_to_freeze"] == 1
        await db.session.refresh(marked)
        await db.session.refresh(legacy)
        assert marked.preparation["token"] != request["token"]
        assert marked.preparation["activation_requested"] is False
        for key in ("author_hint", "state", "error_code", "requested_at", "last_attempt_at"):
            assert marked.preparation[key] == request[key]
        assert legacy.preparation is None
        assert legacy.status == AlertStatus.PAUSED
        again = await tool.freeze(execute=True)
        assert again["requests_to_freeze"] == 0
        assert again["marker_hash"] == result["marker_hash"]


async def test_freeze_refuses_schema_0009_before_any_mutation(monkeypatch):
    async with postgres_replayed_database() as db:
        monkeypatch.setattr(tool, "AsyncSessionLocal", db.maker)
        await db.session.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        await db.session.execute(text("DELETE FROM alembic_version"))
        await db.session.execute(
            text("INSERT INTO alembic_version VALUES ('0009_market_refresh_state')")
        )
        await db.session.commit()
        with pytest.raises(RuntimeError, match="Schéma 0010 requis"):
            await tool.freeze(execute=True)
        assert (await db.session.execute(select(Alert))).scalars().all() == []
