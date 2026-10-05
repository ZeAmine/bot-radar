"""Tests de la métrique de cadence du poller Vinted."""

from app.tasks.vinted_poller import MAX_MEANINGFUL_CYCLE_GAP_S, CycleCadence


def test_cycle_cadence_ignores_the_first_cycle() -> None:
    """Le premier cycle n'ouvre aucun échantillon : son écart porte sur l'init."""
    cadence = CycleCadence()
    cadence.record(1_000.0)

    assert cadence.percentile(50) is None
    assert cadence.percentile(90) is None


def _record_cycles(cadence: CycleCadence, gaps: list[float], start: float = 0.0) -> float:
    """Joue des cycles d'une durée donnée et rend l'horodatage du dernier.

    Le premier `record` sert d'amorce : c'est lui qui ouvre le premier écart.
    """
    now = start
    cadence.record(now)
    for gap in gaps:
        now += gap
        cadence.record(now)
    return now


def test_cycle_cadence_ignores_interruptions() -> None:
    """Une reprise du mode nuit ou une pause anti-blocage ne sont pas des cycles.

    Les inclure ferait afficher une cadence dégradée en p50 alors que la scrutation,
    elle, tourne toujours au rythme prévu.
    """
    cadence = CycleCadence()
    now = _record_cycles(cadence, [15.0, 17.0, 14.0, 16.0])
    # Reprise après une interruption de 3 heures : le p50 doit rester ~15 s.
    cadence.record(now + 3 * 3_600)

    assert cadence.percentile(50) == 15.5
    assert cadence.percentile(90) is not None
    assert cadence.percentile(90) <= MAX_MEANINGFUL_CYCLE_GAP_S


def test_cycle_cadence_ignores_non_positive_gaps() -> None:
    """Un écart nul ou négatif signale une horloge incohérente, pas une cadence."""
    cadence = CycleCadence()
    cadence.record(100.0)
    cadence.record(100.0)
    cadence.record(90.0)

    assert cadence.percentile(50) is None


def test_cycle_cadence_keeps_a_bounded_history() -> None:
    """L'historique est borné, sinon il garderait les cycles de la nuit précédente."""
    cadence = CycleCadence(samples=3)
    _record_cycles(cadence, [10.0, 11.0, 12.0, 13.0])

    assert len(cadence.gaps) == 3
    assert cadence.percentile(50) == 12.0


def test_cycle_cadence_percentiles_are_interpolated() -> None:
    """Percentiles interpolés, comme le p50/p95 de la latence : la même lecture."""
    cadence = CycleCadence()
    _record_cycles(cadence, [10.0, 20.0, 30.0])

    assert cadence.percentile(50) == 20.0
    assert cadence.percentile(90) == 28.0