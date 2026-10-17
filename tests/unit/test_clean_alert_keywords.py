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
from app.domain.rules.keyword_hygiene import Reason  # noqa: E402

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


def test_plan_leaves_exclusions_qualified_by_the_title_alone():
    """Le script réécrit la base : il ne doit pas y retirer un rejet utile.

    « deep it coffret » est exactement l'exclusion que le rapport avait fait
    remonter : le nettoyage ne doit plus l'effacer, sous peine de détruire une
    protection saisie à la main. La variante, elle, reste redondante et part.
    """
    alert = make_alert(
        name="Deep It",
        variants=["deep it delcourt"],
        exclusions=["deep it coffret", "deep it intégrale", "coffret"],
    )

    plan = cck.plan_cleanup(alert)

    assert plan.exclusions_removed == []
    assert plan.exclusions_after == ("deep it coffret", "deep it intégrale", "coffret")
    assert plan.variants_removed == ["deep it delcourt"]


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
        apply=False,
        dry_run=False,
        diff=False,
        yes=False,
        only_degenerate=False,
        json=False,
        limit=None,
        include_inactive=False,
    )

    with patch.object(cck, "read_alerts", AsyncMock(return_value=[alert])):
        code = await cck.run(args)

    assert code == cck.EXIT_CHANGES
    assert alert.variants == FAMILY


async def test_run_apply_cleans_and_returns_clean():
    """En mode écriture, `run` nettoie en base et sort en 0."""
    alert = make_alert(variants=FAMILY, exclusions=["1"])
    session = _FakeSession({str(alert.id): alert})
    args = argparse.Namespace(
        apply=True,
        dry_run=False,
        diff=True,
        yes=False,
        only_degenerate=False,
        json=False,
        limit=None,
        include_inactive=False,
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


def test_the_diff_motives_every_removal():
    """Un diff qui ne dit pas pourquoi ne se relit pas : chaque retrait porte son motif."""
    alert = make_alert(
        variants=["absolute martian manhunter 2a", "9782864971412"],
        exclusions=["1", "x"],
    )

    plan = cck.plan_cleanup(alert)
    motives = {r.raw: r.reason for r in plan.variant_removals + plan.exclusion_removals}

    assert motives["absolute martian manhunter 2a"] == Reason.ENUMERATION
    assert motives["9782864971412"] == Reason.ISBN
    assert motives["1"] == Reason.BARE_TOKEN
    assert motives["x"] == Reason.BARE_TOKEN


def test_the_diff_shows_the_lists_around_each_removal():
    """Le rapport détaillé donne les deux listes, pas seulement ce qui disparaît."""
    alert = make_alert(
        variants=[*FAMILY],
        exclusions=["coffret vide", "1"],
    )

    report = cck.render(
        [cck.plan_cleanup(alert)], apply=False, cleaned_le=INSTANT, diff=True
    )

    assert "exclusions : 2 → 1" in report
    assert "avant : coffret vide, 1" in report
    assert "après : coffret vide" in report
    assert (
        f"{cck.RED}-1{cck.RESET}  {cck.DIM}{Reason.BARE_TOKEN.value}{cck.RESET}"
        in report
    )


def test_only_degenerate_never_consults_the_title():
    """En portée restreinte, un terme que seul le titre écartait reste en place.

    C'est la promesse du mode. « coffret » tient dans le titre « Deep It Coffret »,
    donc il rejetterait l'annonce cible elle-même et le mode complet l'écarte ; la
    portée restreinte s'en abstient, parce que ce jugement dépend du titre.
    """
    alert = make_alert(
        name="Deep It Coffret",
        exclusions=["coffret", "deep it intégrale", "1", "x"],
    )

    complete = cck.plan_cleanup(alert)
    restricted = cck.plan_cleanup(alert, only_degenerate=True)

    assert complete.exclusions_removed == ["coffret", "1", "x"]
    assert restricted.exclusions_removed == ["1", "x"]
    assert restricted.exclusions_after == ("coffret", "deep it intégrale")


def test_only_degenerate_still_removes_a_term_degenerate_on_its_own():
    """Le titre n'est consulté que pour les jugements qui en dépendent vraiment."""
    alert = make_alert(
        variants=["absolute martian manhunter 2a", "9782864971412"],
        exclusions=["1"],
    )

    plan = cck.plan_cleanup(alert, only_degenerate=True)

    assert plan.variants_after == ()
    assert plan.exclusions_after == ()
    assert all(
        removal.reason.is_degenerate
        for removal in plan.variant_removals + plan.exclusion_removals
    )


def test_only_degenerate_does_not_deduplicate():
    """Deux termes identiques restent deux : un rejeu n'a pas à réordonner la liste."""
    alert = make_alert(name="Deep It", exclusions=["coffret", "coffret", "1"])

    plan = cck.plan_cleanup(alert, only_degenerate=True)

    assert plan.exclusions_after == ("coffret", "coffret")
    assert plan.exclusions_removed == ["1"]


async def test_apply_refuses_to_write_a_diff_that_was_never_read():
    """`--apply` seul se refuse : on n'écrit pas ce qu'on n'a pas relu."""
    alert = make_alert(variants=FAMILY)
    args = argparse.Namespace(
        apply=True,
        dry_run=False,
        diff=False,
        yes=False,
        only_degenerate=False,
        json=False,
        limit=None,
        include_inactive=False,
    )

    with (
        patch.object(cck, "read_alerts", AsyncMock(return_value=[alert])),
        patch.object(cck, "AsyncSessionLocal") as session_local,
    ):
        code = await cck.run(args)

    assert code == cck.EXIT_ERROR
    # Aucune lecture, donc surtout aucune écriture.
    session_local.assert_not_called()
    assert alert.variants == FAMILY


async def test_yes_writes_without_the_diff():
    """`--yes` est la seule porte de sortie, et elle engage l'appelant."""
    alert = make_alert(variants=FAMILY, exclusions=["1"])
    session = _FakeSession({str(alert.id): alert})
    args = argparse.Namespace(
        apply=True,
        dry_run=False,
        diff=False,
        yes=True,
        only_degenerate=False,
        json=False,
        limit=None,
        include_inactive=False,
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


async def test_run_restricts_the_write_to_degenerate_terms():
    """`run` propage la portée restreinte jusque dans l'écriture."""
    alert = make_alert(name="Deep It", exclusions=["deep it coffret", "1"])
    session = _FakeSession({str(alert.id): alert})
    args = argparse.Namespace(
        apply=True,
        dry_run=False,
        diff=True,
        yes=False,
        only_degenerate=True,
        json=False,
        limit=None,
        include_inactive=False,
    )

    with (
        patch.object(cck, "read_alerts", AsyncMock(return_value=[alert])),
        patch.object(cck, "AsyncSessionLocal", lambda: session),
    ):
        code = await cck.run(args)

    assert code == cck.EXIT_CLEAN
    assert alert.exclude_keywords == ["deep it coffret"]


def test_as_json_carries_the_motifs():
    """La sortie machine est motivée comme l'écran, et dit la portée qu'elle a jouée."""
    alert = make_alert(variants=FAMILY, exclusions=["1"])

    payload = json.loads(
        cck.as_json([cck.plan_cleanup(alert, only_degenerate=True)], apply=False,
                    cleaned_le=INSTANT)
    )

    assert payload["portee"] == "degenere"
    motifs = payload["alertes"][0]["motifs"]
    assert {m["terme"]: m["raison"] for m in motifs["exclusions"]}["1"] == (
        Reason.BARE_TOKEN.value
    )
    assert all(m["degenere"] for m in motifs["variantes"] + motifs["exclusions"])
