"""L'occurrence transmise au job est celle de l'exécution déclenchée.

Ces tests font tourner un **vrai** `AsyncIOScheduler` avec son exécuteur asyncio :
c'est la seule façon de vérifier que l'instant reçu par la tâche est bien celui de
l'exécution en cours. Une lecture de `job.next_run_time` ne suffisait pas — cette
valeur est avancée dès que l'exécution suivante est planifiée, et le test l'a
démontré : au moment où la tâche s'exécutait, elle portait déjà l'heure de la
prochaine passe, pas celle de l'exécution déclenchée.
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN
from apscheduler.triggers.cron import CronTrigger

import app.tasks.scheduler  # noqa: F401 - nécessaire pour résoudre le module ci-dessous

scheduler_module = sys.modules["app.tasks.scheduler"]


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
    captures: list[dict[str, Any]] = []
    captured, stopped = asyncio.Event(), asyncio.Event()

    async def tache_espionne(**kwargs: Any) -> dict[str, int]:
        captures.append(kwargs)
        captured.set()
        return {}

    monkeypatch.setattr(scheduler_module, "refresh_book_prices_task", tache_espionne)

    scheduler = scheduler_module.create_scheduler()
    job = scheduler.get_job("refresh_prices_07_00")
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    assert job.max_instances == 1
    assert job.coalesce is True
    original_job = job.func

    async def isolated_occurrence_job() -> dict[str, int]:
        return await original_job()

    for other in scheduler.get_jobs():
        if other.id != job.id:
            scheduler.remove_job(other.id)
    job.modify(func=isolated_occurrence_job, misfire_grace_time=5)
    scheduler.add_listener(lambda _event: stopped.set(), EVENT_SCHEDULER_SHUTDOWN)
    try:
        scheduler.start(paused=True)
        vise = datetime.now(UTC) + (retard or timedelta())
        job.modify(next_run_time=vise)

        scheduler.resume()
        await asyncio.wait_for(captured.wait(), timeout=timeout)
        prochaine = job.next_run_time
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
            await asyncio.wait_for(stopped.wait(), timeout=timeout)

    assert captures, "le scheduler n'a pas déclenché le job"
    return {"appel": captures[0], "vise": vise, "prochaine": prochaine}


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

    C'est le défaut que la lecture de `next_run_time` introduisait : à l'exécution,
    cette valeur porte déjà l'heure de la passe suivante. Le test compare donc
    l'instant reçu à la prochaine exécution du job et exige qu'ils diffèrent — et
    que l'instant reçu colle à l'exécution déclenchée.
    """
    resultat = await _declencher_et_capturer(monkeypatch)

    recu = resultat["appel"].get("scheduled_for")
    assert recu is not None

    prochaine = resultat["prochaine"]
    # La prochaine passe du cron de 07:00 est un autre moment de la journée.
    assert prochaine is not None
    assert recu.astimezone(UTC) != prochaine.astimezone(UTC)

    ecart_a_lexecution = abs(
        (recu.astimezone(UTC) - resultat["vise"].astimezone(UTC)).total_seconds()
    )
    assert ecart_a_lexecution < 1.0


@pytest.mark.asyncio
async def test_a_delayed_run_is_still_identified_by_its_own_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une exécution retardée garde l'occurrence de l'exécution déclenchée.

    Le job est lancé avec un instant de déclenchement déjà passé : l'exécution est
    donc « en retard ». L'instant transmis doit rester celui de l'exécution qui a
    effectivement tourné — et, dans la limite du regroupement d'APScheduler, celui
    de l'exécution due —, jamais une heure future présentée comme la sienne.
    """
    resultat = await _declencher_et_capturer(monkeypatch, retard=timedelta(seconds=-1))

    recu = resultat["appel"].get("scheduled_for")
    assert recu is not None

    # L'instant transmis ne peut pas être dans le futur : ce serait présenter une
    # heure de prochaine exécution comme l'heure prévue de l'exécution en cours.
    maintenant = datetime.now(UTC)
    assert recu.astimezone(UTC) <= maintenant + timedelta(seconds=2), (
        f"l'occurrence transmise ({recu.isoformat()}) est dans le futur"
    )
