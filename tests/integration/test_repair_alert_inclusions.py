"""La réparation applique un plan relu atomiquement et refuse un état périmé."""

import uuid

import pytest
from sqlalchemy import select

from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.user import User
from scripts.repair_alert_inclusions import apply_plan
from tests.database import postgres_replayed_database


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_inclusion_repair_is_atomic_and_idempotent(conflict):
    async with postgres_replayed_database() as database:
        maker = database.maker
        async with maker() as session:
            user = User(telegram_user_id=89898, is_allowed=True)
            category = Category(slug="bd_comics", label="Livres", emoji="📚")
            session.add_all([user, category])
            await session.flush()
            alerts = [Alert(
                id=uuid.uuid4(), user_id=user.id, category_id=category.id,
                name=name, include_keywords=[name.lower(), author],
                exclude_keywords=["coffret"], variants=[], status="paused",
            ) for name, author in [("Nanbanjin", "pedrosa"), ("Deep It", "mathieu")]]
            session.add_all(alerts)
            await session.commit()
            plan = [{
                "id": str(a.id), "name": a.name, "isbn": None,
                "includes_before": a.include_keywords, "includes_after": [a.name.lower()],
                "variants_before": [], "variants_after": [],
            } for a in alerts]
            if conflict:
                alerts[1].include_keywords = ["choix manuel"]
                await session.commit()
        if conflict:
            with pytest.raises(ValueError, match="périmé"):
                async with maker() as session, session.begin():
                    await apply_plan(session, plan)
        else:
            async with maker() as session, session.begin():
                assert await apply_plan(session, plan) == 2
            async with maker() as session, session.begin():
                assert await apply_plan(session, plan) == 0
        async with maker() as session:
            stored = list((await session.scalars(select(Alert).order_by(Alert.name))).all())
            assert [a.include_keywords for a in stored] == (
                [["choix manuel"], ["nanbanjin", "pedrosa"]] if conflict
                else [["deep it"], ["nanbanjin"]]
            )
            assert all(a.exclude_keywords == ["coffret"] for a in stored)
            assert all(a.status == "paused" for a in stored)
