"""Le journal des réglages : une ligne par champ modifié, quel que soit le chemin.

Chaque test conduit un chemin d'écriture réel — le bot, une tâche de fond, un script
de masse — sur une vraie base, puis relit le journal. Ce qui est vérifié n'est pas
qu'une fonction a été appelée, mais que la ligne porte le champ, l'ancienne valeur,
la nouvelle et l'origine du geste qui l'a posée.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.db.models  # noqa: F401  (le journal doit vivre dans `Base.metadata`)
from app.bot.handlers import alerts_create, autotune
from app.bot.handlers import alerts_manage as edit
from app.db.alert_settings_journal import JOURNALED_FIELDS
from app.db.models.alert import Alert
from app.db.models.alert_setting_change import AlertSettingChange
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.alert_repo import AlertRepository
from app.domain.enums import AlertStatus
from app.services.ai.modules.keyword_variants import KeywordVariantsOutcome
from scripts import apply_audit_plan, clean_alert_keywords, repair_alert_inclusions
from tests.add_fakes import DraftState
from tests.database import postgres_replayed_database

ISBN = "9782369903086"
TITLE = "La couleur des choses"
FIRST_CONDITION, SECOND_CONDITION = edit.STANDARD_CONDITIONS[:2]
# Le produit crée l'alerte avec le titre comme premier mot-clé inclus.
INCLUDES = ["la couleur des choses"]


@pytest.fixture
async def world(monkeypatch) -> AsyncGenerator[SimpleNamespace, None]:
    """Deux alertes réelles, et les fabriques de session des modules concernés."""
    async with postgres_replayed_database() as db:
        db.maker.configure(autoflush=False)
        monkeypatch.setattr("app.db.session.AsyncSessionLocal", db.maker)
        monkeypatch.setattr(edit, "AsyncSessionLocal", db.maker)
        monkeypatch.setattr(clean_alert_keywords, "AsyncSessionLocal", db.maker)
        monkeypatch.setattr(
            "app.services.book_market_pricing.recalculate_invalidated_series", AsyncMock()
        )
        category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        user = User(telegram_user_id=907, is_allowed=True)
        db.session.add_all([category, user])
        await db.session.flush()
        alert = Alert(
            user_id=user.id,
            category_id=category.id,
            name=TITLE,
            isbn=ISBN,
            include_keywords=list(INCLUDES),
            variants=["variantes initiales"],
            exclude_keywords=["coffret"],
            max_item_price_cents=1500,
            accepted_conditions=[FIRST_CONDITION],
            status=AlertStatus.ACTIVE,
            listing_baseline={"state": "pending"},
        )
        # Une alerte dont l'ISBN figure dans ses listes, comme le produit les crée :
        # c'est le seul cas où un changement d'ISBN change réellement des réglages.
        isbn_alert = Alert(
            user_id=user.id,
            category_id=category.id,
            name="Autre ouvrage",
            isbn=ISBN,
            include_keywords=["autre", ISBN],
            variants=["autre variante", ISBN],
            exclude_keywords=["integrale", ISBN],
            max_item_price_cents=2000,
            status=AlertStatus.ACTIVE,
        )
        db.session.add_all([alert, isbn_alert])
        await db.session.commit()
        yield SimpleNamespace(
            db=db, alert=alert, isbn_alert=isbn_alert, user=user, category=category
        )


async def journal(
    world: SimpleNamespace, alert_id: uuid.UUID | None = None
) -> list[tuple[str, object, object, str]]:
    """Les lignes du journal d'une alerte, triées par champ.

    Le tri par champ suffit : aucun test ne modifie deux fois le même champ, et il
    rend la lecture indépendante de la granularité de l'horodatage.
    """
    async with world.db.maker() as session:
        rows = (
            await session.scalars(
                select(AlertSettingChange)
                .where(AlertSettingChange.alert_id == (alert_id or world.alert.id))
                .order_by(AlertSettingChange.field)
            )
        ).all()
    return [(row.field, row.old_value, row.new_value, row.origin) for row in rows]


def callback(data: str) -> AsyncMock:
    """Un clic Telegram dont le corps de rappel est celui du vrai clavier."""
    query = AsyncMock()
    query.data = data
    return query


async def test_prix_modifie_par_le_bot_est_journalise(world):
    state = DraftState()
    state.data = {"edit_alert_id": str(world.alert.id), "edit_field": "max_price"}
    message = AsyncMock()
    message.text = "9,90"

    async with world.db.maker() as session:
        await edit.save_edited_field_value(message, state, world.user, session)

    assert await journal(world) == [
        (
            "max_item_price_cents",
            1500,
            990,
            "app.bot.handlers.alerts_manage._save_edited_field_value",
        )
    ]


async def test_mots_cles_modifies_par_le_bot_sont_journalises(world):
    state = DraftState()
    state.data = {"edit_alert_id": str(world.alert.id), "edit_field": "include_keywords"}
    message = AsyncMock()
    message.text = "couleur, choses, panchaud"

    async with world.db.maker() as session:
        await edit.save_edited_field_value(message, state, world.user, session)

    assert await journal(world) == [
        (
            "include_keywords",
            INCLUDES,
            ["couleur", "choses", "panchaud"],
            "app.bot.handlers.alerts_manage._save_edited_field_value",
        )
    ]


async def test_retrait_d_isbn_journalise_une_ligne_par_champ_touche(world, monkeypatch):
    """Un seul geste, trois réglages : chacun sa ligne, avec son ancienne valeur."""
    monkeypatch.setattr(edit, "prepare_bd_alert", AsyncMock(return_value=False))
    monkeypatch.setattr(edit, "price_book_now", AsyncMock())
    monkeypatch.setattr(
        "app.services.book_market_pricing.schedule_book_pricing", lambda *_, **__: None
    )
    state = DraftState()
    state.data = {"edit_alert_id": str(world.isbn_alert.id), "edit_field": "isbn"}
    message = AsyncMock()
    message.text = "-"

    async with world.db.maker() as session:
        await edit.save_edited_field_value(message, state, world.user, session)

    origin = "app.bot.handlers.alerts_manage._save_edited_field_value"
    assert await journal(world, world.isbn_alert.id) == [
        ("exclude_keywords", ["integrale", ISBN], ["integrale"], origin),
        ("include_keywords", ["autre", ISBN], ["autre"], origin),
        ("variants", ["autre variante", ISBN], ["autre variante"], origin),
    ]


async def test_bascule_d_une_condition_est_journalisee(world):
    async with world.db.maker() as session:
        await edit.handle_edit_condition_toggle(
            callback(f"ed_c_tog:{world.alert.id}:1"), world.user, session
        )

    assert await journal(world) == [
        (
            "accepted_conditions",
            [FIRST_CONDITION],
            [FIRST_CONDITION, SECOND_CONDITION],
            "app.bot.handlers.alerts_manage.handle_edit_condition_toggle",
        )
    ]


async def test_acceptation_de_tous_les_etats_est_journalisee(world):
    async with world.db.maker() as session:
        await edit.handle_edit_condition_all(
            callback(f"ed_c_all:{world.alert.id}"), world.user, session
        )

    assert await journal(world) == [
        (
            "accepted_conditions",
            [FIRST_CONDITION],
            None,
            "app.bot.handlers.alerts_manage.handle_edit_condition_all",
        )
    ]


async def test_application_des_exclusions_d_autotune_est_journalisee(world, monkeypatch):
    monkeypatch.setattr(autotune, "pop_pending_tuning", AsyncMock(return_value=["coffret", "lot"]))
    monkeypatch.setattr(autotune, "clear_dislikes_for_alert", AsyncMock(return_value=2))

    async with world.db.maker() as session:
        await autotune.handle_tune_apply(
            callback(f"ai_tune_apply:{world.alert.id}"), world.user, session
        )

    assert await journal(world) == [
        (
            "exclude_keywords",
            ["coffret"],
            ["coffret", "lot"],
            "app.bot.handlers.autotune.handle_tune_apply",
        )
    ]


async def test_application_des_variantes_d_autotune_est_journalisee(world):
    autotune.save_pending_variants(world.alert.id, ["couleurs des choses"])
    try:
        async with world.db.maker() as session:
            await autotune.handle_var_apply(
                callback(f"ai_var_apply:{world.alert.id}"), world.user, session
            )
    finally:
        autotune.pop_pending_variants(world.alert.id)

    assert await journal(world) == [
        (
            "variants",
            ["variantes initiales"],
            ["variantes initiales", "couleurs des choses"],
            "app.bot.handlers.autotune.handle_var_apply",
        )
    ]


async def test_prix_actualise_depuis_le_recap_de_creation_est_journalise(world):
    async with world.db.maker() as session:
        await alerts_create.handle_update_existing_alert_price(
            callback(f"upd_alert_price:{world.alert.id}:2500"), DraftState(), session
        )

    assert await journal(world) == [
        (
            "max_item_price_cents",
            1500,
            2500,
            "app.bot.handlers.alerts_create.handle_update_existing_alert_price",
        )
    ]


async def test_prix_saisi_pour_une_alerte_existante_est_journalise(world):
    state = DraftState()
    state.data = {"existing_alert_id": str(world.alert.id)}
    message = AsyncMock()
    message.text = "30"

    async with world.db.maker() as session:
        await alerts_create.process_max_price_input(message, state, session)

    assert await journal(world) == [
        (
            "max_item_price_cents",
            1500,
            3000,
            "app.bot.handlers.alerts_create.process_max_price_input",
        )
    ]


async def test_rafraichissement_des_mots_cles_en_tache_de_fond_est_journalise(world, monkeypatch):
    monkeypatch.setattr(
        edit.keyword_variants_module,
        "generate_variants_and_exclusions",
        AsyncMock(
            return_value=KeywordVariantsOutcome(
                variants=["couleurs des choses"],
                exclusions=["integrale"],
                answered=True,
            )
        ),
    )

    await edit._refresh_alert_keywords(
        world.alert.id,
        expected_isbn=ISBN,
        name=TITLE,
        category_slug="bd_comics",
        book_context=None,
        target=AsyncMock(),
    )

    origin = "app.bot.handlers.alerts_manage._refresh_alert_keywords"
    assert await journal(world) == [
        ("exclude_keywords", ["coffret"], ["coffret", "integrale"], origin),
        (
            "variants",
            ["variantes initiales"],
            ["variantes initiales", "couleurs des choses"],
            origin,
        ),
    ]


async def test_initialisation_du_catalogue_de_reference_est_journalisee(world):
    """Le geste de masse du poller fige un catalogue : le journal doit le dire."""
    scan_started_at = world.alert.created_at
    async with world.db.maker() as session:
        await AlertRepository(session).initialize_listing_baselines(
            "bd_comics", "", ["v1", "v2"], scan_started_at
        )
        await session.commit()

    assert await journal(world) == [
        (
            "listing_baseline",
            {"state": "pending"},
            {
                "state": "ready",
                "started_at": scan_started_at.isoformat(),
                "external_ids": ["v1", "v2"],
            },
            "app.db.repositories.alert_repo.AlertRepository.initialize_listing_baselines",
        )
    ]


async def test_nettoyage_de_masse_des_mots_cles_est_journalise(world):
    plan = clean_alert_keywords.AlertKeywordPlan(
        alert_id=str(world.alert.id),
        name=TITLE,
        variants_before=("variantes initiales",),
        variants_after=("couleurs des choses",),
        exclusions_before=("coffret",),
        exclusions_after=("coffret",),
    )

    assert await clean_alert_keywords.persist([plan]) == 1

    assert await journal(world) == [
        (
            "variants",
            ["variantes initiales"],
            ["couleurs des choses"],
            "scripts.clean_alert_keywords.apply_plan",
        )
    ]


async def test_reparation_des_inclusions_est_journalisee(world):
    plan = [
        {
            "id": str(world.alert.id),
            "name": TITLE,
            "isbn": ISBN,
            "deleted": False,
            "includes_before": list(INCLUDES),
            "includes_after": ["la couleur des choses panchaud"],
            "variants_before": ["variantes initiales"],
            "variants_after": ["couleurs des choses"],
        }
    ]

    async with world.db.maker() as session:
        assert await repair_alert_inclusions.apply_plan(session, plan) == 1
        await session.commit()

    origin = "scripts.repair_alert_inclusions.apply_plan"
    assert await journal(world) == [
        ("include_keywords", INCLUDES, ["la couleur des choses panchaud"], origin),
        ("variants", ["variantes initiales"], ["couleurs des choses"], origin),
    ]


async def test_application_d_un_plan_d_audit_est_journalisee(world):
    entry = {
        "id": str(world.alert.id),
        "name": TITLE,
        "field": "max_item_price_cents",
        "before": 1500,
        "after": 1200,
        "reason": "plafond au-dessus du marché",
    }

    async with world.db.maker() as session:
        verdict = await apply_audit_plan._apply_entry(
            session, entry, apply=True, already_removed=set()
        )
        await session.commit()

    assert verdict["status"] == "corrigé"
    assert await journal(world) == [
        ("max_item_price_cents", 1500, 1200, "scripts.apply_audit_plan._apply_entry")
    ]


async def test_ecriture_sans_changement_n_ecrit_rien(world, monkeypatch):
    """Reposer la même exclusion, réappliquer le même plan, réécrire la même liste."""
    monkeypatch.setattr(autotune, "pop_pending_tuning", AsyncMock(return_value=["coffret"]))
    monkeypatch.setattr(autotune, "clear_dislikes_for_alert", AsyncMock(return_value=0))
    async with world.db.maker() as session:
        await autotune.handle_tune_apply(
            callback(f"ai_tune_apply:{world.alert.id}"), world.user, session
        )

    unchanged_plan = [
        {
            "id": str(world.alert.id),
            "name": TITLE,
            "isbn": ISBN,
            "deleted": False,
            "includes_before": list(INCLUDES),
            "includes_after": list(INCLUDES),
            "variants_before": ["variantes initiales"],
            "variants_after": ["variantes initiales"],
        }
    ]
    async with world.db.maker() as session:
        assert await repair_alert_inclusions.apply_plan(session, unchanged_plan) == 0
        alert = await AlertRepository(session).get_by_id(world.alert.id)
        alert.exclude_keywords = ["coffret"]
        alert.include_keywords = list(INCLUDES)
        alert.max_item_price_cents = 1500
        alert.accepted_conditions = [FIRST_CONDITION]
        await session.commit()

    assert await journal(world) == []


async def test_creation_d_alerte_n_ecrit_aucune_ligne(world):
    """Une création n'a pas d'ancienne valeur : elle n'invente pas d'historique."""
    async with world.db.maker() as session:
        created = await AlertRepository(session).create(
            user_id=world.user.id,
            category_id=world.category.id,
            name="Nouvelle",
            include_keywords=["nouvelle"],
            max_item_price_cents=4200,
        )
        created_id = created.id
        await session.commit()

    assert await journal(world, created_id) == []


async def test_ecriture_hors_reglages_n_est_pas_journalisee(world):
    """Statut, nom, cote de série : le journal ne consigne que les réglages."""
    async with world.db.maker() as session:
        alert = await AlertRepository(session).get_by_id(world.alert.id)
        alert.status = AlertStatus.PAUSED
        alert.name = "Nouveau nom"
        alert.series_total_cents = 9000
        alert.vinted_search_url = "https://www.vinted.fr/catalog?search_text=nouveau"
        alert.book_search_exhausted_at = None
        alert.include_keywords = list(INCLUDES)
        await session.commit()

    assert await journal(world) == []


def test_le_journal_est_indexe_par_alerte_et_horodatage() -> None:
    """La lecture utile — l'histoire d'une alerte — doit tenir sur un index."""
    indexes = {
        index.name: [column.name for column in index.columns]
        for index in AlertSettingChange.__table__.indexes
    }
    assert indexes.get("ix_alert_setting_changes_alert_id_changed_at") == [
        "alert_id",
        "changed_at",
    ]


def test_l_alerte_supprimee_emporte_son_journal() -> None:
    """Aucune ligne orpheline : la clé étrangère suit la suppression de l'alerte."""
    (foreign_key,) = AlertSettingChange.__table__.c.alert_id.foreign_keys
    assert foreign_key.column.table.name == "alerts"
    assert foreign_key.ondelete == "CASCADE"


def test_l_ecriture_est_branchee_une_seule_fois_sur_la_session() -> None:
    """Le journal s'installe avec la session applicative, jamais dans un handler."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    import app.db.session  # noqa: F401  (cet import installe le point d'écriture)
    from app.db.alert_settings_journal import journal_alert_setting_changes

    assert event.contains(Session, "before_flush", journal_alert_setting_changes)


async def test_migration_cree_et_retire_le_journal() -> None:
    """La migration est réversible et son schéma est celui du modèle."""
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).resolve().parents[2] / "alembic/versions/0016_alert_setting_changes.py"
    spec = spec_from_file_location("alert_setting_changes_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    async with postgres_replayed_database() as db, db.session.bind.begin() as conn:

        def round_trip(connection):
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                inspector = sa.inspect(connection)
                assert "alert_setting_changes" not in inspector.get_table_names()
                migration.upgrade()
                inspector = sa.inspect(connection)
                assert "alert_setting_changes" in inspector.get_table_names()
                assert {
                    column["name"] for column in inspector.get_columns("alert_setting_changes")
                } == {column.name for column in AlertSettingChange.__table__.columns}
                assert "ix_alert_setting_changes_alert_id_changed_at" in {
                    index["name"] for index in inspector.get_indexes("alert_setting_changes")
                }

        await conn.run_sync(round_trip)


def test_les_champs_suivis_sont_ceux_du_contrat() -> None:
    """Un champ ajouté au suivi sans être déclaré ici laisserait un trou d'historique."""
    assert set(JOURNALED_FIELDS) == {
        "include_keywords",
        "exclude_keywords",
        "variants",
        "max_item_price_cents",
        "accepted_conditions",
        "listing_baseline",
    }
