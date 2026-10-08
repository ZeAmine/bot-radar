"""Migration additive et limite explicite du retour à un code ignorant les intentions."""

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import column, select, table, text, update

from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.user import User
from app.domain.entities.alert_preparation import PreparationRequest
from app.domain.enums import AlertStatus
from tests.database import postgres_replayed_database


async def test_additive_migration_leaves_old_rows_null_and_refuses_destructive_downgrade():
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "alembic/versions/0010_alert_preparation.py"
    spec = spec_from_file_location("preparation_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with postgres_replayed_database() as db:
        category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        user = User(telegram_user_id=601, is_allowed=True)
        db.session.add_all([category, user])
        await db.session.flush()
        for status in (AlertStatus.ACTIVE, AlertStatus.PAUSED):
            db.session.add(
                Alert(
                    user_id=user.id,
                    category_id=category.id,
                    name=str(status),
                    status=status,
                    include_keywords=[],
                )
            )
        await db.session.commit()
        async with db.session.bind.begin() as conn:
            await conn.execute(text("ALTER TABLE alerts DROP COLUMN preparation"))

            def upgrade(connection):
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                    with pytest.raises(RuntimeError, match="aucun downgrade destructeur"):
                        migration.downgrade()

            await conn.run_sync(upgrade)
        async with db.maker() as session:
            rows = (await session.execute(select(Alert).order_by(Alert.name))).scalars().all()
            assert [row.status for row in rows] == [AlertStatus.ACTIVE, AlertStatus.PAUSED]
            assert all(row.preparation is None for row in rows)


async def test_old_orm_can_read_but_its_resume_bypasses_even_a_frozen_request():
    """La maintenance bloquant les anciennes commandes est obligatoire, pas facultative."""
    async with postgres_replayed_database() as db:
        category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        user = User(telegram_user_id=601, is_allowed=True)
        db.session.add_all([category, user])
        await db.session.flush()
        request = PreparationRequest(
            state="blocked", error_code="catalog_write", author_hint="Auteur demandé"
        )
        frozen = request.suspend()
        assert frozen.token != request.token
        assert frozen.author_hint == request.author_hint
        assert frozen.state == "blocked"
        assert frozen.error_code == "catalog_write"
        alert = Alert(
            user_id=user.id,
            category_id=category.id,
            name="Préparation nouvelle",
            include_keywords=[],
            status=AlertStatus.PAUSED,
            preparation=frozen.stored(),
        )
        db.session.add(alert)
        await db.session.commit()
        # Projection des deux colonnes connues de l'ancien chemin de statut.
        old = table("alerts", column("id", Alert.id.type), column("status", Alert.status.type))
        assert (
            await db.session.execute(select(old.c.status).where(old.c.id == alert.id))
        ).scalar_one() == AlertStatus.PAUSED
        await db.session.execute(
            update(old).where(old.c.id == alert.id).values(status=AlertStatus.ACTIVE)
        )
        await db.session.commit()
        await db.session.refresh(alert)
        assert alert.status == AlertStatus.ACTIVE  # Danger démontré de /resume ancien.
        assert alert.preparation == frozen.stored()
