"""Le script de nettoyage, testé sur une vraie alerte et le vrai nettoyeur.

Le plan est calculé par le nettoyeur de `app.domain.rules.keyword_hygiene` : ces tests
ne le remplacent pas par un mock, ils l'exécutent. L'écriture est vérifiée sur une
session factice, parce que ce qui compte est la mutation appliquée et le commit — pas
Postgres lui-même.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import clean_alert_keywords as cck  # noqa: E402

from app.core.constants import AlertStatus  # noqa: E402
from app.db.models.alert import Alert  # noqa: E402

INSTANT = datetime(2026, 10, 5, 19, 30, 0, tzinfo=UTC)

FAMILY = [f"absolute martian manhunter 2{letter}" for letter in "abc"]


def make_alert(
    *,
    name: str = "Absolute Martian Manhunter Vol. 2",
    variants: list[str] | None = None,
    exclusions: list[str] | None = None,
    status: AlertStatus = AlertStatus.ACTIVE,
) -> Alert:
    """Une alerte réelle, construite comme le fait le bot avant persistance."""
    return Alert(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        category_id=uuid.uuid4(),
        name=name,
        include_keywords=["absolute martian manhunter"],
        variants=variants if variants is not None else [],
        exclude_keywords=exclusions if exclusions is not None else [],
        status=status,
    )


def test_plan_keeps_only_usable_terms():
    """Le plan retire l'énumération et l'exclusion numérique, garde le reste."""
    alert = make_alert(
        variants=["absolute martian manhunter 2", "absolute martian manhunter tome 2", *FAMILY],
        exclusions=["1", "coffret vide"],
    )

    plan = cck.plan_cleanup(alert)

    assert plan.changed is True
    assert plan.variants_after == (
        "absolute martian manhunter 2",
        "absolute martian manhunter tome 2",
    )
    assert plan.variants_removed == FAMILY
    assert plan.exclusions_after == ("coffret vide",)
    assert plan.exclusions_removed == ["1"]


def test_apply_then_replan_is_idempotent():
    """Une seconde passe ne trouve plus rien : le nettoyage est idempotent."""
    alert = make_alert(variants=FAMILY, exclusions=["1"])

    first = cck.plan_cleanup(alert)
    cck.apply_plan(alert, first)
    assert alert.variants == list(first.variants_after)
    assert alert.exclude_keywords == list(first.exclusions_after)

    second = cck.plan_cleanup(alert)
    assert second.changed is False
    assert second.variants_removed == []
    assert second.exclusions_removed == []


def test_clean_alert_has_nothing_to_change():
    """Une alerte saine n'est pas signalée."""
    alert = make_alert(variants=["absolute martian manhunter tome 2"], exclusions=["coffret vide"])

    assert cck.plan_cleanup(alert).changed is False


def test_render_names_what_is_removed():
    """Le rapport nomme les termes retirés, sans quoi personne ne peut le contester."""
    alert = make_alert(variants=FAMILY, exclusions=["1"])

    report = cck.render([cck.plan_cleanup(alert)], apply=False, cleaned_le=INSTANT)

    assert "rapport (aucune écriture)" in report
    assert "absolute martian manhunter 2a" in report
    assert "1 alerte(s) à nettoyer" in report


def test_as_json_carries_the_same_removals():
    """La sortie machine porte l'en-tête et les retraits, pas un tableau nu."""
    alert = make_alert(variants=FAMILY, exclusions=["1"])

    payload = json.loads(
        cck.as_json([cck.plan_cleanup(alert)], apply=True, cleaned_le=INSTANT)
    )

    assert payload["mode"] == "ecriture"
    assert payload["population"]["lues"] == 1
    assert payload["alertes"][0]["variants"]["retirees"] == FAMILY
    assert payload["alertes"][0]["exclusions"]["retirees"] == ["1"]


class _FakeSession:
    """Session minimale : relit par identifiant, mute, commite."""

    def __init__(self, alerts: dict[str, Alert]) -> None:
        self._alerts = alerts
        self.committed = False

    async def get(self, _model: object, key: object) -> Alert | None:
        return self._alerts.get(str(key))

    async def commit(self) -> None:
        self.committed = True

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def test_persist_writes_changed_alert_and_commits():
    """L'écriture porte sur l'alerte relue, et le commit a lieu une fois."""
    alert = make_alert(variants=FAMILY, exclusions=["1"])
    session = _FakeSession({str(alert.id): alert})
    plan = cck.plan_cleanup(alert)

    with patch.object(cck, "AsyncSessionLocal", lambda: session):
        written = await cck.persist([plan])

    assert written == 1
    assert alert.variants == []
    assert alert.exclude_keywords == []
    assert session.committed is True


async def test_run_reports_changes_without_writing_by_default():
    """En mode rapport, `run` sort en 1 et n'ouvre aucune session."""
    alert = make_alert(variants=FAMILY)
    args = argparse.Namespace(
        apply=False, dry_run=False, json=False, limit=None, include_inactive=False
    )

    with patch.object(cck, "read_alerts", AsyncMock(return_value=[alert])):
        code = await cck.run(args)

    assert code == cck.EXIT_CHANGES
    # Aucune écriture : l'alerte en mémoire est intacte.
    assert alert.variants == FAMILY


async def test_run_apply_cleans_and_returns_clean():
    """En mode écriture, `run` nettoie en base et sort en 0."""
    alert = make_alert(variants=FAMILY, exclusions=["1"])
    session = _FakeSession({str(alert.id): alert})
    args = argparse.Namespace(
        apply=True, dry_run=False, json=False, limit=None, include_inactive=False
    )

    with (
        patch.object(cck, "read_alerts", AsyncMock(return_value=[alert])),
        patch.object(cck, "AsyncSessionLocal", lambda: session),
    ):
        code = await cck.run(args)

    assert code == cck.EXIT_CLEAN
    assert alert.variants == []
    assert alert.exclude_keywords == []
    assert session.committed is True
