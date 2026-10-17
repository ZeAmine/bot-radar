"""L'occurrence transmise au job est celle de l'exécution déclenchée.

Ces tests font tourner un **vrai** `AsyncIOScheduler` avec son exécuteur asyncio :
c'est la seule façon de vérifier que l'instant reçu par la tâche est bien celui de
l'exécution en cours. Une lecture de `job.next_run_time` ne suffisait pas — cette
valeur est avancée dès que l'exécution suivante est planifiée, et le test l'a
démontré : au moment où la tâche s'exécutait, elle portait déjà l'heure de la
prochaine passe, pas celle de l'exécution déclenchée.

`create_scheduler()` enregistre des jobs de production. Toutes leurs opérations
métier sont remplacées ici, avant tout démarrage : le câblage réel (cron,
`EVENT_JOB_SUBMITTED`, file d'occurrence par job, exécuteur) reste exercé, mais
aucune collecte, cotation, reprise ni écriture n'est déclenchée.
`_verifier_jobs_sans_operation_metier_reelle` refuse de démarrer si un job non
substitué subsiste.
"""

import asyncio
import importlib
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest
from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN
from apscheduler.triggers.cron import CronTrigger

import app.tasks.scheduler  # noqa: F401 - nécessaire pour résoudre le module ci-dessous

scheduler_module = sys.modules["app.tasks.scheduler"]

# Jobs planifiés par `create_scheduler()` et opération métier à neutraliser : un job
# non couvert fait échouer le helper plutôt que de s'exécuter réellement.
_OPERATIONS_METIER = (
    "recover_bd_preparations",
    "recover_notifications",
    "cleanup_old_data_task",
    "run_proactive_autotune_scan",
    "refresh_book_prices_task",
)


def _substituer_operations_metier(
    monkeypatch: pytest.MonkeyPatch,
    appels: list[str],
    rafraichissements: list[dict[str, Any]],
    observations: dict[str, Any],
) -> dict[str, Any]:
    """Remplace chaque opération métier atteignable par un espion local.

    Les espions n'ouvrent aucune session, ne cotent rien et ne collectent rien.
    `refresh_book_prices_task` est celui qui reçoit l'occurrence : c'est l'objet
    du test, il est espionné, jamais réimplémenté.

    Renvoie la table des espions installés, pour vérifier ensuite qu'aucun job
    construit ne pointe encore vers une fonction applicative.
    """
    espions: dict[str, Any] = {}

    def fabriquer(nom: str, collecte: list[Any] | None) -> Any:
        async def _espion(*_args: Any, **kwargs: Any) -> dict[str, int]:
            appels.append(nom)
            if collecte is not None:
                collecte.append(kwargs)
                observations["captured"].set()
                # `next_run_time` est lu pendant l'exécution, comme le ferait une
                # implémentation fautive : il doit différer de l'occurrence transmise.
                scheduler = observations.get("scheduler")
                if scheduler is not None:
                    job = scheduler.get_job("refresh_prices_07_00")
                    observations["prochaine_execution"] = getattr(
                        job, "next_run_time", None
                    )
            return {}

        # Nom lisible ; la vérification s'appuie sur l'identité de l'espion, pas sur son nom.
        _espion.__name__ = f"espion_{nom}"
        return _espion

    for attribut in _OPERATIONS_METIER:
        espion = fabriquer(
            attribut,
            rafraichissements if attribut == "refresh_book_prices_task" else None,
        )
        espions[attribut] = espion
        monkeypatch.setattr(scheduler_module, attribut, espion)
    return espions


def _verifier_jobs_sans_operation_metier_reelle(
    scheduler: Any, espions: dict[str, Any]
) -> None:
    """Refuse de démarrer si un job non substitué subsiste.

    Un job dont la fonction vient encore du code applicatif exécuterait une collecte,
    une cotation ou une écriture : le test doit échouer **avant** le démarrage, pas
    après coup. L'identité est comparée aux espions réellement installés.
    """
    substituees = set(map(id, espions.values()))
    for job in scheduler.get_jobs():
        if id(job.func) in substituees:
            continue
        # `job_avec_occurrence` est la fermeture réelle : elle porte le mécanisme
        # d'occurrence à vérifier et n'appelle que des noms du module, substitués.
        assert getattr(job.func, "__name__", "") == "job_avec_occurrence", (
            f"job non substitué : {job.id} ({getattr(job.func, '__name__', '?')}) "
            f"pointe vers {getattr(job.func, '__module__', '?')} et pourrait exécuter "
            "du travail réel"
        )


async def _declencher_et_capturer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    retard: timedelta | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Démarre un vrai scheduler, déclenche le job de rafraîchissement, capture l'appel.

    `retard` vise l'instant de déclenchement : `None` déclenche « maintenant »,
    une valeur négative simule une exécution en retard.
    """
    appels: list[str] = []
    rafraichissements: list[dict[str, Any]] = []
    captured, stopped = asyncio.Event(), asyncio.Event()
    observations: dict[str, Any] = {"captured": captured}

    # Les opérations métier sont substituées avant `create_scheduler()` : celui-ci
    # capture les noms du module à l'appel, un scheduler construit trop tôt les fige.
    espions = _substituer_operations_metier(
        monkeypatch, appels, rafraichissements, observations
    )
    scheduler = scheduler_module.create_scheduler()
    observations["scheduler"] = scheduler
    _verifier_jobs_sans_operation_metier_reelle(scheduler, espions)

    job = scheduler.get_job("refresh_prices_07_00")
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    assert job.max_instances == 1
    assert job.coalesce is True
    for other in scheduler.get_jobs():
        if other.id != job.id:
            scheduler.remove_job(other.id)
    job.modify(misfire_grace_time=5)
    scheduler.add_listener(lambda _event: stopped.set(), EVENT_SCHEDULER_SHUTDOWN)
    try:
        scheduler.start(paused=True)
        vise = datetime.now(UTC) + (retard or timedelta())
        job.modify(next_run_time=vise)
        scheduler.resume()
        await asyncio.wait_for(captured.wait(), timeout=timeout)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
            await asyncio.wait_for(stopped.wait(), timeout=timeout)

    assert rafraichissements, (
        f"le scheduler n'a pas déclenché le job (jobs invoqués : {appels})"
    )
    return {
        "appel": rafraichissements[0],
        "vise": vise,
        "prochaine_execution": observations.get("prochaine_execution"),
        "appels": appels,
    }


@pytest.mark.asyncio
async def test_the_real_scheduler_hands_the_intended_run_time_to_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le scheduler déclenche le job et lui transmet l'instant qu'il visait.

    On ne se contente pas d'appeler l'enveloppe : le job part d'un cron réel et
    c'est l'exécuteur qui l'invoque. L'instant reçu est comparé à celui que le
    déclenchement visait, à la tolérance d'ordonnancement près.
    """
    resultat = await _declencher_et_capturer(monkeypatch)

    recu = resultat["appel"].get("scheduled_for")
    assert recu is not None, "le job doit recevoir l'instant prévu"
    ecart = abs((recu.astimezone(UTC) - resultat["vise"].astimezone(UTC)).total_seconds())
    assert ecart < 1.0, f"instant reçu à {ecart:.3f}s de l'exécution déclenchée"


@pytest.mark.asyncio
async def test_the_transmitted_time_is_the_triggered_run_not_the_next_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L'instant transmis est celui de **cette** exécution, pas de la suivante.

    C'est le défaut que la lecture de `next_run_time` introduirait : à l'exécution,
    cette valeur porte déjà l'heure de la passe suivante. On l'observe donc **depuis
    le job lui-même**, au moment où il tourne, et on exige qu'elle diffère de
    l'instant transmis — sinon l'assertion ne prouverait rien.
    """
    resultat = await _declencher_et_capturer(monkeypatch)

    recu = resultat["appel"].get("scheduled_for")
    assert recu is not None

    ecart_a_lexecution = abs(
        (recu.astimezone(UTC) - resultat["vise"].astimezone(UTC)).total_seconds()
    )
    assert ecart_a_lexecution < 1.0

    # La prochaine exécution du cron de 07:00 ne peut pas être l'occurrence transmise.
    prochaine = resultat["prochaine_execution"]
    assert prochaine is not None, "la prochaine exécution doit être observable"
    assert recu.astimezone(UTC) != prochaine.astimezone(UTC), (
        "l'instant transmis est la prochaine exécution, pas l'exécution déclenchée"
    )
    assert prochaine.astimezone(UTC) > recu.astimezone(UTC)


@pytest.mark.asyncio
async def test_a_delayed_run_is_still_identified_by_its_own_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une exécution retardée garde l'occurrence de l'exécution déclenchée.

    Le job est déclenché avec un instant déjà passé : l'exécution est donc « en
    retard ». L'instant transmis doit rester celui de l'exécution due, jamais une
    heure future présentée comme la sienne. L'instant visé est comparé directement,
    sans se contenter de vérifier qu'il n'est pas futur.
    """
    resultat = await _declencher_et_capturer(monkeypatch, retard=timedelta(seconds=-1))

    recu = resultat["appel"].get("scheduled_for")
    assert recu is not None

    maintenant = datetime.now(UTC)
    assert recu.astimezone(UTC) <= maintenant + timedelta(seconds=2), (
        f"l'occurrence transmise ({recu.isoformat()}) est dans le futur"
    )
    ecart = abs((recu.astimezone(UTC) - resultat["vise"].astimezone(UTC)).total_seconds())
    assert ecart < 1.0, (
        f"l'occurrence retardée est à {ecart:.3f}s de l'instant dû, "
        "ce n'est pas celle de l'exécution déclenchée"
    )


def test_the_scheduler_occurrence_module_stays_importable() -> None:
    """Le module est résolu par nom : une correction de câblage ne doit pas le casser."""
    assert isinstance(scheduler_module, ModuleType)
    assert hasattr(scheduler_module, "create_scheduler")
    assert importlib.import_module("app.tasks.scheduler") is scheduler_module
