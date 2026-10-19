from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.services import alert_chain_state as state
from app.services.alert_fix import core, policy


def _sign(payload: dict[str, Any], key: bytes) -> str:
    if len(key) < 32:
        raise ValueError("clé d'authentification insuffisante")
    return hmac.new(key, core.canonical_json(payload).encode(), hashlib.sha256).hexdigest()


def verify_envelope(envelope: dict[str, Any], *, key: bytes, now: datetime) -> core.Ticket:
    if set(envelope) != {"version", "task_id", "approval_expires_at", "ticket", "signature"}:
        raise ValueError("enveloppe invalide")
    payload = {k: v for k, v in envelope.items() if k != "signature"}
    signature = envelope["signature"]
    if not isinstance(signature, str) or not hmac.compare_digest(_sign(payload, key), signature):
        raise ValueError("origine non authentifiée")
    if envelope["version"] != 1:
        raise ValueError("version inconnue")
    ticket = core.Ticket.from_dict(envelope["ticket"])
    if ticket.provenance != "approved_state":
        raise ValueError("provenance de ticket incompatible")
    if ticket.compute_hash() != ticket.ticket_hash:
        raise ValueError("ticket altéré")
    if not re.fullmatch(r"[0-9a-f]{40}", ticket.base_sha):
        raise ValueError("SHA complet requis")
    expiry = core.parse_datetime(envelope["approval_expires_at"], field="approval_expires_at")
    if not ticket.issued_at <= now < ticket.expires_at <= expiry:
        raise ValueError("autorisation ou ticket expiré")
    policy.validate_allowed_files(ticket, ())
    return ticket


def _authority(
    directory: Path, alert_id: str, now: datetime, allowed_user_ids: set[int], valid_seconds: int
) -> tuple[state.Incident, state.Decision]:
    if not 1 <= valid_seconds <= 86400:
        raise ValueError("validité d'approbation invalide")
    if state.is_suspended(directory):
        raise ValueError("maintenance suspendue")
    state.validate_authority(directory)
    incident = state.read_incidents(directory).get(alert_id)
    decision = state.read_decisions(directory).get(alert_id)
    if incident is None or incident.status != state.IncidentStatus.OPEN:
        raise ValueError("incident absent ou fermé")
    if (
        decision is None
        or decision.kind != state.DecisionKind.APPROVED
        or decision.user_id not in allowed_user_ids
        or isinstance(decision.user_id, bool)
    ):
        raise ValueError("décision non applicable")
    if (
        not incident.opened_at
        <= decision.decided_at
        <= now
        < decision.decided_at + timedelta(seconds=valid_seconds)
    ):
        raise ValueError("approbation expirée ou incohérente")
    return incident, decision


def _jobs(directory: Path) -> list[dict[str, Any]]:
    records = state.authoritative_records(directory / "tasks.jsonl")
    for record in records:
        if not all(
            k in record for k in ("task_id", "status", "envelope", "decision_id", "attempt")
        ):
            raise ValueError("journal de tâches invalide")
        if record["status"] not in {"reserved", "started", "interrupted", "completed"}:
            raise ValueError("statut de tâche invalide")
    return records


def _latest(directory: Path, task_id: str) -> dict[str, Any]:
    job = next((v for v in reversed(_jobs(directory)) if v["task_id"] == task_id), None)
    if job is None:
        raise ValueError("tâche inconnue")
    return job


def _append(directory: Path, job: dict[str, Any], now: datetime) -> None:
    state._atomic_append(directory / "tasks.jsonl", {**job, "at": now.isoformat()})


def export_task(
    *,
    directory: Path,
    alert_id: str,
    now: datetime,
    key: bytes,
    allowed_user_ids: set[int],
    base_sha: str,
    allowed_files: tuple[str, ...],
    evidence_files: tuple[str, ...],
    evidence_hashes: dict[str, str],
    confirmed_scope_hash: str,
    test_file: str,
    trace: dict[str, Any],
    approval_valid_seconds: int,
    ticket_ttl_seconds: int,
) -> dict[str, Any]:
    scope_hash = scope_digest(
        base_sha=base_sha,
        allowed_files=allowed_files,
        evidence_files=evidence_files,
        evidence_hashes=evidence_hashes,
        test_file=test_file,
    )
    if not hmac.compare_digest(scope_hash, confirmed_scope_hash):
        raise ValueError("listes et contexte non confirmés par l'opérateur")
    if not 1 <= ticket_ttl_seconds <= 14400:
        raise ValueError("durée du ticket invalide")
    with state.exclusive_lock(directory):
        incident, decision = _authority(
            directory, alert_id, now, allowed_user_ids, approval_valid_seconds
        )
        if not re.fullmatch(r"[0-9a-f]{40}", base_sha) or base_sha != incident.commit:
            raise ValueError("SHA non confirmé par l'incident")
        if decision.user_id is None:
            raise ValueError("décideur absent")
        decision_id = core.sha256_text(
            core.canonical_json(
                {
                    "alert_id": alert_id,
                    "user_id": decision.user_id,
                    "decided_at": decision.decided_at.isoformat(),
                    "kind": decision.kind.value,
                }
            )
        )
        if any(v["decision_id"] == decision_id for v in _jobs(directory)):
            raise ValueError("décision déjà réservée")
        expiry = decision.decided_at + timedelta(seconds=approval_valid_seconds)
        ticket = core.Ticket(
            schema_version=core.SCHEMA_VERSION,
            ticket_id=uuid.uuid4().hex,
            ticket_hash="",
            alert_id=alert_id,
            incident=core.Incident(
                incident.status.value,
                incident.opened_at,
                incident.commit,
                incident.severity,
                incident.reason,
            ),
            decision=core.Decision(decision.kind.value, decision.decided_at, decision.user_id),
            issued_at=now,
            expires_at=min(now + timedelta(seconds=ticket_ttl_seconds), expiry),
            base_sha=base_sha,
            allowed_files=allowed_files,
            test_file=test_file,
            evidence_files=evidence_files,
            evidence_hashes=dict(evidence_hashes),
            provenance="approved_state",
            trace=trace,
            limits=core.Limits(),
        )
        ticket = replace(ticket, ticket_hash=ticket.compute_hash())
        envelope = {
            "version": 1,
            "task_id": uuid.uuid4().hex,
            "approval_expires_at": expiry.isoformat(),
            "ticket": ticket.to_dict(),
        }
        envelope["signature"] = _sign(envelope, key)
        verify_envelope(envelope, key=key, now=now)
        _append(
            directory,
            {
                "task_id": envelope["task_id"],
                "status": "reserved",
                "envelope": envelope,
                "decision_id": decision_id,
                "attempt": 0,
                "approval_valid_seconds": approval_valid_seconds,
            },
            now,
        )
        return envelope


def start_task(
    *,
    directory: Path,
    envelope: dict[str, Any],
    key: bytes,
    now: datetime,
    allowed_user_ids: set[int],
) -> int:
    ticket = verify_envelope(envelope, key=key, now=now)
    with state.exclusive_lock(directory):
        job = _latest(directory, envelope["task_id"])
        if job["envelope"] != envelope or job["status"] != "reserved":
            raise ValueError("réservation incompatible")
        incident, decision = _authority(
            directory, ticket.alert_id, now, allowed_user_ids, job["approval_valid_seconds"]
        )
        if (
            incident.commit != ticket.base_sha
            or decision.decided_at != ticket.decision.decided_at
            or decision.user_id != ticket.decision.user_id
        ):
            raise ValueError("autorité modifiée")
        if type(job["attempt"]) is not int or job["attempt"] < 0:
            raise ValueError("compteur de tâche invalide")
        attempt: int = job["attempt"] + 1
        _append(directory, {**job, "status": "started", "attempt": attempt}, now)
        return attempt


def interrupt_task(
    *, directory: Path, task_id: str, attempt: int, now: datetime, confirmed_stopped: bool
) -> None:
    with state.exclusive_lock(directory):
        job = _latest(directory, task_id)
        if not confirmed_stopped or job["status"] != "started" or job["attempt"] != attempt:
            raise ValueError("arrêt non confirmé")
        _append(directory, {**job, "status": "interrupted"}, now)


def resume_task(
    *,
    directory: Path,
    task_id: str,
    now: datetime,
    authorize_retry: bool,
    key: bytes,
    allowed_user_ids: set[int],
) -> None:
    with state.exclusive_lock(directory):
        job = _latest(directory, task_id)
        ticket = verify_envelope(job["envelope"], key=key, now=now)
        _authority(directory, ticket.alert_id, now, allowed_user_ids, job["approval_valid_seconds"])
        if not authorize_retry or job["status"] != "interrupted" or state.is_suspended(directory):
            raise ValueError("reprise non autorisée")
        _append(directory, {**job, "status": "reserved"}, now)


def complete_task(
    *,
    directory: Path,
    task_id: str,
    attempt: int,
    now: datetime,
    result: str,
    report_digest: str,
    candidate_digest: str | None,
) -> None:
    if result not in {"ready_for_review", "failed"}:
        raise ValueError("résultat de préparation invalide")
    if not re.fullmatch(r"[0-9a-f]{64}", report_digest):
        raise ValueError("empreinte du rapport invalide")
    if result == "ready_for_review" and (
        candidate_digest is None or not re.fullmatch(r"[0-9a-f]{64}", candidate_digest)
    ):
        raise ValueError("empreinte du candidat requise")
    with state.exclusive_lock(directory):
        job = _latest(directory, task_id)
        if job["status"] != "started" or job["attempt"] != attempt:
            raise ValueError("tentative incompatible")
        _append(
            directory,
            {
                **job,
                "status": "completed",
                "result": result,
                "base_sha": job["envelope"]["ticket"]["base_sha"],
                "report_digest": report_digest,
                "candidate_digest": candidate_digest,
            },
            now,
        )


def scope_digest(
    *,
    base_sha: str,
    allowed_files: tuple[str, ...],
    evidence_files: tuple[str, ...],
    evidence_hashes: dict[str, str],
    test_file: str,
) -> str:
    policy.validate_context_files(evidence_files, evidence_hashes)
    return core.sha256_text(
        core.canonical_json(
            {
                "base_sha": base_sha,
                "allowed_files": list(allowed_files),
                "evidence_files": list(evidence_files),
                "evidence_hashes": evidence_hashes,
                "test_file": test_file,
            }
        )
    )
