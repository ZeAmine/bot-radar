"""Tests de l'état durable de la maintenance et des décisions humaines."""

import json
import multiprocessing
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_alert_chain import claim_on_disk, read_records  # noqa: E402

from app.services import alert_chain_state as state  # noqa: E402
from app.services.alert_chain_state import (  # noqa: E402
    DecisionKind,
    DecisionOutcome,
    IncidentStatus,
    consecutive_failures,
    expire_stale_requests,
    find_open_incident,
    is_approved,
    is_suspended,
    mark_technical_alert_sent,
    read_decisions,
    record_incident,
    record_resolution,
    record_technical_failure,
    refuse_approval,
    request_approval,
    reset_technical_failures,
    resume_maintenance,
    suspend_maintenance,
    technical_alert_sent,
)


def _open(directory: Path, *, alert_id: str = "a1", now: datetime | None = None) -> datetime:
    moment = now or datetime.now(UTC)
    record_incident(
        directory=directory,
        alert_id=alert_id,
        commit="abc123",
        severity="critical",
        reason="correspondances actionnables en retard",
        now=moment,
    )
    return moment


def _read_incident_in_child(path: str, out) -> None:
    """Relit un incident depuis un processus séparé, sans état partagé en mémoire."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from app.services import alert_chain_state as child_state

    incident = child_state.find_open_incident(Path(path))
    out.put(incident.alert_id if incident else None)


def _claim_in_child(path: str, out) -> None:
    """Tente une réservation depuis un processus séparé, sans Redis."""
    import check_alert_chain as child_script

    claimed = child_script.claim_on_disk(
        directory=Path(path),
        key="concurrent",
        ttl_seconds=3600,
        now=datetime.now(UTC),
    )
    out.put(claimed)


# ---------------------------------------------------------------------------
# Persistance entre exécutions séparées
# ---------------------------------------------------------------------------


def test_incident_identity_survives_separate_processes(tmp_path):
    """Deux processus distincts relisent le même incident, avec le même identifiant."""
    directory = tmp_path / "state"
    now = datetime.now(UTC)
    _open(directory, alert_id="stable-id", now=now)

    # Relecture depuis un processus séparé : l'identité ne dépend pas de la mémoire.
    # Une file `multiprocessing` passe par un tube du système, pas par un socket :
    # la recette isolée interdit les connexions réseau et compte les refus.
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_read_incident_in_child, args=(str(directory), queue))
    process.start()
    process.join(timeout=30)
    exitcode = process.exitcode
    observed = queue.get(timeout=5)
    queue.close()

    assert exitcode == 0
    assert observed == "stable-id"


def test_incident_and_decision_survive_restart(tmp_path):
    """Après redémarrage, l'incident et la décision sont reconstruits des journaux."""
    directory = tmp_path / "state"
    now = datetime.now(UTC)
    _open(directory, alert_id="a1", now=now)
    assert request_approval(
        directory=directory, alert_id="a1", user_id=42, now=now, ttl_seconds=3600
    ).outcome is DecisionOutcome.RECORDED

    incident = find_open_incident(directory)
    assert incident is not None
    assert incident.alert_id == "a1"
    assert incident.status is IncidentStatus.OPEN
    assert is_approved(directory, "a1") is True

    decisions = read_decisions(directory)
    assert decisions["a1"].kind is DecisionKind.APPROVED
    assert decisions["a1"].user_id == 42


def test_journal_is_append_only_and_reconstructible(tmp_path):
    """Le journal conserve chaque fait ; l'état courant en est déduit."""
    directory = tmp_path / "state"
    now = datetime.now(UTC)
    _open(directory, alert_id="a1", now=now)
    record_resolution(directory=directory, alert_id="a1", commit="abc123", now=now)

    lines = (directory / state.INCIDENTS_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["status"] == IncidentStatus.OPEN.value
    assert json.loads(lines[1])["status"] == IncidentStatus.VERIFIED.value
    assert find_open_incident(directory) is None


def test_corrupt_line_is_ignored_without_losing_history(tmp_path):
    """Une ligne illisible ne fait pas perdre les incidents valides."""
    directory = tmp_path / "state"
    now = datetime.now(UTC)
    _open(directory, alert_id="a1", now=now)
    journal = directory / state.INCIDENTS_FILE
    journal.write_text(
        journal.read_text(encoding="utf-8") + "{ ligne tronquée\n", encoding="utf-8"
    )

    incident = find_open_incident(directory)
    assert incident is not None
    assert incident.alert_id == "a1"


# ---------------------------------------------------------------------------
# Dédoublonnage inter-processus
# ---------------------------------------------------------------------------


def test_dedup_holds_across_separate_executions_without_redis(tmp_path):
    """Sans Redis, deux exécutions séparées ne notifient pas deux fois."""
    directory = tmp_path / "state"
    directory.mkdir(parents=True)
    now = datetime.now(UTC)

    first = claim_on_disk(
        directory=directory, key="radar:alert_chain:incident:notified",
        ttl_seconds=3600, now=now,
    )
    second = claim_on_disk(
        directory=directory, key="radar:alert_chain:incident:notified",
        ttl_seconds=3600, now=now + timedelta(minutes=1),
    )

    assert first is True
    assert second is False


def test_dedup_expires_after_ttl(tmp_path):
    """Passé le délai, un nouveau créneau peut être réservé."""
    directory = tmp_path / "state"
    directory.mkdir(parents=True)
    now = datetime.now(UTC)
    claim_on_disk(
        directory=directory, key="k", ttl_seconds=60, now=now
    )
    assert claim_on_disk(
        directory=directory, key="k", ttl_seconds=60, now=now + timedelta(seconds=61)
    ) is True


def test_concurrent_claims_from_separate_processes_yield_single_winner(tmp_path):
    """Le verrou de fichier sérialise les réservations concurrentes."""
    directory = tmp_path / "state"
    directory.mkdir(parents=True)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_claim_in_child, args=(str(directory), queue))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
    results = [queue.get(timeout=5) for _ in processes]
    queue.close()

    assert sum(1 for item in results if item) == 1


# ---------------------------------------------------------------------------
# Décisions humaines
# ---------------------------------------------------------------------------


def test_approval_is_bound_to_its_alert_id(tmp_path):
    """Une autorisation porte sur un incident précis, jamais sur un autre."""
    directory = tmp_path / "state"
    now = datetime.now(UTC)
    _open(directory, alert_id="a1", now=now)
    _open(directory, alert_id="a2", now=now)

    request_approval(
        directory=directory, alert_id="a1", user_id=42, now=now, ttl_seconds=3600
    )

    assert is_approved(directory, "a1") is True
    assert is_approved(directory, "a2") is False


def test_double_click_is_refused(tmp_path):
    """Une seconde décision sur le même incident n'écrase pas la première."""
    directory = tmp_path / "state"
    now = datetime.now(UTC)
    _open(directory, alert_id="a1", now=now)

    first = request_approval(
        directory=directory, alert_id="a1", user_id=42, now=now, ttl_seconds=3600
    )
    second = request_approval(
        directory=directory, alert_id="a1", user_id=42, now=now, ttl_seconds=3600
    )

    assert first.outcome is DecisionOutcome.RECORDED
    assert second.outcome is DecisionOutcome.ALREADY_DECIDED
    decisions = [
        line
        for line in (directory / state.DECISIONS_FILE).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(decisions) == 1


def test_decision_after_refusal_is_refused(tmp_path):
    """Un refus déjà enregistré ne peut pas être remplacé par une autorisation."""
    directory = tmp_path / "state"
    now = datetime.now(UTC)
    _open(directory, alert_id="a1", now=now)

    assert refuse_approval(
        directory=directory, alert_id="a1", user_id=42, now=now
    ).outcome is DecisionOutcome.RECORDED
    assert request_approval(
        directory=directory, alert_id="a1", user_id=42, now=now, ttl_seconds=3600
    ).outcome is DecisionOutcome.ALREADY_DECIDED
    assert is_approved(directory, "a1") is False


def test_expired_request_records_nothing_actionable(tmp_path):
    """Une demande hors délai est consignée expirée et n'autorise rien."""
    directory = tmp_path / "state"
    opened = datetime.now(UTC) - timedelta(hours=5)
    _open(directory, alert_id="a1", now=opened)

    result = request_approval(
        directory=directory,
        alert_id="a1",
        user_id=42,
        now=datetime.now(UTC),
        ttl_seconds=3600,
    )

    assert result.outcome is DecisionOutcome.EXPIRED
    assert is_approved(directory, "a1") is False
    assert read_decisions(directory)["a1"].kind is DecisionKind.EXPIRED


def test_decision_on_resolved_incident_has_no_effect(tmp_path):
    """Un incident résolu n'accepte plus aucune décision."""
    directory = tmp_path / "state"
    now = datetime.now(UTC)
    _open(directory, alert_id="a1", now=now)
    record_resolution(directory=directory, alert_id="a1", commit="abc123", now=now)

    result = request_approval(
        directory=directory, alert_id="a1", user_id=42, now=now, ttl_seconds=3600
    )

    assert result.outcome is DecisionOutcome.WRONG_STATE
    assert is_approved(directory, "a1") is False


def test_unknown_incident_is_refused(tmp_path):
    """Un identifiant inconnu n'enregistre strictement rien."""
    directory = tmp_path / "state"
    directory.mkdir(parents=True)
    result = request_approval(
        directory=directory,
        alert_id="inconnu",
        user_id=42,
        now=datetime.now(UTC),
        ttl_seconds=3600,
    )
    assert result.outcome is DecisionOutcome.WRONG_STATE
    assert read_decisions(directory) == {}


def test_stale_requests_are_expired_once(tmp_path):
    """L'expiration ne réécrit pas une décision déjà prise."""
    directory = tmp_path / "state"
    opened = datetime.now(UTC) - timedelta(hours=5)
    _open(directory, alert_id="pending", now=opened)
    _open(directory, alert_id="decided", now=opened)
    request_approval(
        directory=directory,
        alert_id="decided",
        user_id=42,
        now=opened + timedelta(minutes=1),
        ttl_seconds=3600,
    )

    now = datetime.now(UTC)
    expired = expire_stale_requests(directory=directory, now=now, ttl_seconds=3600)
    again = expire_stale_requests(directory=directory, now=now, ttl_seconds=3600)

    assert expired == ["pending"]
    assert again == []
    assert read_decisions(directory)["decided"].kind is DecisionKind.APPROVED


# ---------------------------------------------------------------------------
# Suspension et reprise
# ---------------------------------------------------------------------------


def test_suspension_and_resume_are_explicit(tmp_path):
    """La suspension s'active, se relit, et n'est levée que sur reprise explicite."""
    directory = tmp_path / "state"
    now = datetime.now(UTC)
    _open(directory, alert_id="a1", now=now)

    assert is_suspended(directory) is False
    suspend_maintenance(directory=directory, user_id=42, now=now, reason="opérateur")
    assert is_suspended(directory) is True
    # Les incidents ouverts sont conservés pendant la suspension.
    assert find_open_incident(directory).alert_id == "a1"
    assert is_suspended(directory) is True

    resume_maintenance(directory=directory, user_id=42, now=now, reason="opérateur")
    assert is_suspended(directory) is False


def test_suspension_is_journalised(tmp_path):
    """Une suspension et une reprise restent traçables durablement."""
    directory = tmp_path / "state"
    now = datetime.now(UTC)
    suspend_maintenance(directory=directory, user_id=7, now=now, reason="arrêt")
    resume_maintenance(directory=directory, user_id=7, now=now, reason="reprise")

    kinds = [
        json.loads(line)["kind"]
        for line in (directory / state.DECISIONS_FILE).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert kinds == [DecisionKind.SUSPENDED.value, DecisionKind.RESUMED.value]


# ---------------------------------------------------------------------------
# Contrôles impossibles
# ---------------------------------------------------------------------------


def test_technical_failures_are_counted_and_reset(tmp_path):
    """Le compteur s'incrémente, puis se réinitialise après un contrôle réussi."""
    directory = tmp_path / "state"
    directory.mkdir(parents=True)
    now = datetime.now(UTC)

    for expected in range(1, 4):
        failures = record_technical_failure(
            directory=directory, now=now, detail="OperationalError"
        )
        assert failures == expected

    reset_technical_failures(directory=directory)
    assert consecutive_failures(directory) == 0


def test_technical_alert_is_bounded(tmp_path):
    """Une alerte technique envoyée n'est pas renvoyée sans nouveau contrôle réussi."""
    directory = tmp_path / "state"
    directory.mkdir(parents=True)
    now = datetime.now(UTC)

    assert technical_alert_sent(directory) is False
    mark_technical_alert_sent(directory=directory, now=now)
    assert technical_alert_sent(directory) is True

    for _ in range(state.TECHNICAL_FAILURE_THRESHOLD):
        record_technical_failure(directory=directory, now=now, detail="OperationalError")
    # Le drapeau reste posé : le silence se poursuit jusqu'à une reprise réussie.
    assert technical_alert_sent(directory) is True

    reset_technical_failures(directory=directory)
    assert technical_alert_sent(directory) is False


def test_technical_state_never_closes_an_incident(tmp_path):
    """Un contrôle impossible laisse l'incident strictement ouvert."""
    directory = tmp_path / "state"
    directory.mkdir(parents=True)
    now = datetime.now(UTC)
    _open(directory, alert_id="a1", now=now)

    for _ in range(state.TECHNICAL_FAILURE_THRESHOLD):
        record_technical_failure(directory=directory, now=now, detail="OperationalError")

    incident = find_open_incident(directory)
    assert incident is not None
    assert incident.status is IncidentStatus.OPEN


def test_read_records_tolerates_absent_file(tmp_path):
    """La trace JSONL absente n'est pas une erreur."""
    assert read_records(tmp_path / "absent.jsonl") == []


def test_atomic_write_leaves_no_partial_file(tmp_path):
    """Aucun fichier temporaire ne subsiste après une écriture."""
    directory = tmp_path / "state"
    now = datetime.now(UTC)
    _open(directory, alert_id="a1", now=now)
    request_approval(
        directory=directory, alert_id="a1", user_id=42, now=now, ttl_seconds=60
    )
    leftovers = [item.name for item in directory.iterdir() if item.name.startswith(".")]
    assert leftovers == [state.LOCK_FILE]
