"""État durable de la maintenance de la chaîne d'alerte.

Le détecteur tourne dans des conteneurs jetables (`docker compose run --rm`) tandis
que les boutons d'approbation vivent dans le processus `uvicorn` : sans support
partagé, un `alert_id` ne survivrait pas d'une exécution à l'autre et aucune
décision ne pourrait être relue. Ce module fournit ce support sous la forme d'un
répertoire monté.

Autorité des données — une seule par question :

- `incidents.jsonl` fait autorité sur l'état des incidents ;
- `decisions.jsonl` fait autorité sur les décisions humaines ;
- `suspension.json`, `technical.json` portent respectivement l'état de suspension
  et le compteur de contrôles impossibles.

Les deux journaux sont *append-only* : une interruption ne peut donc perdre qu'une
écriture non terminée, jamais réécrire l'histoire. L'état courant est reconstruit
par relecture complète, ce qui rend tout fichier dérivé inutile — il n'en existe
aucun ici, et c'est délibéré.

Écritures atomiques (`tempfile` + `os.replace`) et verrou `flock` inter-processus :
le détecteur et le bot peuvent écrire le même répertoire sans se corrompre.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger("radar.alert_chain.state")

DEFAULT_STATE_DIR = Path("/var/lib/botradar")
STATE_DIR_ENV = "ALERT_CHAIN_STATE_DIR"

INCIDENTS_FILE = "incidents.jsonl"
DECISIONS_FILE = "decisions.jsonl"
SUSPENSION_FILE = "suspension.json"
TECHNICAL_FILE = "technical.json"
LOCK_FILE = ".lock"

TECHNICAL_FAILURE_THRESHOLD = 4
DEFAULT_APPROVAL_TTL_SECONDS = 86400


class IncidentStatus(StrEnum):
    """Statut d'un incident, décidé par le symptôme et non par un correctif."""

    OPEN = "ouvert"
    VERIFIED = "vérifié"


class DecisionKind(StrEnum):
    """Décision humaine rattachée à un incident."""

    APPROVED = "approuvé"
    REFUSED = "refusé"
    EXPIRED = "expiré"
    SUSPENDED = "suspendu"
    RESUMED = "repris"


class DecisionOutcome(StrEnum):
    """Résultat de la tentative d'enregistrement d'une décision."""

    RECORDED = "recorded"
    ALREADY_DECIDED = "already_decided"
    WRONG_STATE = "wrong_state"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Incident:
    """Incident tel que reconstruit depuis le journal."""

    alert_id: str
    status: IncidentStatus
    opened_at: datetime
    commit: str
    severity: str
    reason: str


@dataclass(frozen=True)
class Decision:
    """Décision humaine telle que reconstruite depuis le journal."""

    alert_id: str
    kind: DecisionKind
    decided_at: datetime
    user_id: int | None
    outcome: DecisionOutcome


@dataclass(frozen=True)
class ApprovalResult:
    """Verdict d'une demande d'approbation, avec son motif exploitable."""

    outcome: DecisionOutcome
    reason: str
    incident: Incident | None = None


def state_dir() -> Path:
    """Répertoire d'état partagé, surchargeable pour les tests."""
    configured = os.environ.get(STATE_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_STATE_DIR


def read_json_object(path: Path) -> dict[str, Any]:
    """Lit un objet JSON d'état, ou rend un dictionnaire vide s'il est illisible."""
    return _read_json_object(path)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Écrit un objet JSON d'état de façon atomique."""
    _write_json_atomic(path, payload)


def parse_datetime(raw: Any) -> datetime | None:
    """Interprète un horodatage d'état, en supposant UTC s'il est naïf."""
    return _parse_datetime(raw)


@contextmanager
def exclusive_lock(directory: Path) -> Iterator[None]:
    """Verrou inter-processus du répertoire d'état."""
    with _exclusive_lock(directory):
        yield


def _parse_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@contextmanager
def _exclusive_lock(directory: Path) -> Iterator[None]:
    """Sérialise les écritures entre processus via un verrou de fichier."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / LOCK_FILE).open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_replace(path: Path, content: str) -> None:
    """Écrit `content` puis bascule le fichier, sans état intermédiaire visible.

    Un `append` direct n'est pas atomique vis-à-vis d'un lecteur concurrent : une
    ligne partielle serait relue comme corrompue. La réécriture suivie d'un
    `os.replace` expose toujours un fichier complet.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = handle.name
    try:
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _atomic_append(path: Path, payload: dict[str, Any]) -> None:
    """Ajoute une ligne au journal, en préservant son contenu existant."""
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"
    encoded = json.dumps(payload, ensure_ascii=False)
    _atomic_replace(path, f"{existing}{encoded}\n")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _atomic_replace(path, json.dumps(payload, ensure_ascii=False) + "\n")


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    """Relit un journal ; une ligne illisible est ignorée, jamais fatale."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            logger.warning("Ligne illisible ignorée dans %s", path.name)
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        logger.warning("Fichier d'état illisible ignoré : %s", path.name)
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


def record_incident(
    *,
    directory: Path,
    alert_id: str,
    commit: str,
    severity: str,
    reason: str,
    now: datetime,
) -> Incident:
    """Ouvre un incident, ou le laisse ouvert s'il l'était déjà."""
    incident = Incident(
        alert_id=alert_id,
        status=IncidentStatus.OPEN,
        opened_at=now,
        commit=commit,
        severity=severity,
        reason=reason,
    )
    payload = {
        "event": "incident",
        "alert_id": alert_id,
        "status": IncidentStatus.OPEN.value,
        "opened_at": now.isoformat(),
        "commit": commit,
        "severity": severity,
        "reason": reason,
    }
    with _exclusive_lock(directory):
        _atomic_append(directory / INCIDENTS_FILE, payload)
    return incident


def record_resolution(
    *,
    directory: Path,
    alert_id: str,
    commit: str,
    now: datetime,
) -> None:
    """Clôt un incident parce que le symptôme n'est plus reproduit."""
    payload = {
        "event": "incident",
        "alert_id": alert_id,
        "status": IncidentStatus.VERIFIED.value,
        "resolved_at": now.isoformat(),
        "commit": commit,
    }
    with _exclusive_lock(directory):
        _atomic_append(directory / INCIDENTS_FILE, payload)


def read_incidents(directory: Path) -> dict[str, Incident]:
    """Reconstruit l'état des incidents par relecture du journal."""
    incidents: dict[str, Incident] = {}
    order: list[str] = []
    for record in _read_json_lines(directory / INCIDENTS_FILE):
        if record.get("event") != "incident":
            continue
        alert_id = record.get("alert_id")
        if not isinstance(alert_id, str):
            continue
        status_raw = record.get("status")
        if status_raw == IncidentStatus.VERIFIED.value:
            existing = incidents.get(alert_id)
            if existing is not None:
                incidents[alert_id] = Incident(
                    alert_id=alert_id,
                    status=IncidentStatus.VERIFIED,
                    opened_at=existing.opened_at,
                    commit=existing.commit,
                    severity=existing.severity,
                    reason=existing.reason,
                )
            continue
        if status_raw != IncidentStatus.OPEN.value:
            continue
        opened_at = _parse_datetime(record.get("opened_at")) or datetime.now(UTC)
        if alert_id not in incidents:
            order.append(alert_id)
        incidents[alert_id] = Incident(
            alert_id=alert_id,
            status=IncidentStatus.OPEN,
            opened_at=opened_at,
            commit=str(record.get("commit") or "inconnu"),
            severity=str(record.get("severity") or "critical"),
            reason=str(record.get("reason") or ""),
        )
    return {alert_id: incidents[alert_id] for alert_id in order}


def find_open_incident(directory: Path) -> Incident | None:
    """Rend l'incident ouvert le plus récent, ou `None` si aucun ne l'est."""
    open_incidents = [
        item
        for item in read_incidents(directory).values()
        if item.status is IncidentStatus.OPEN
    ]
    if not open_incidents:
        return None
    return max(open_incidents, key=lambda item: item.opened_at)


# ---------------------------------------------------------------------------
# Décisions
# ---------------------------------------------------------------------------


def read_decisions(directory: Path) -> dict[str, Decision]:
    """Reconstruit la dernière décision connue par incident."""
    decisions: dict[str, Decision] = {}
    for record in _read_json_lines(directory / DECISIONS_FILE):
        if record.get("event") != "decision":
            continue
        alert_id = record.get("alert_id")
        kind_raw = record.get("kind")
        if not isinstance(alert_id, str) or not isinstance(kind_raw, str):
            continue
        try:
            kind = DecisionKind(kind_raw)
        except ValueError:
            logger.warning("Décision de nature inconnue ignorée : %s", kind_raw)
            continue
        decided_at = _parse_datetime(record.get("decided_at")) or datetime.now(UTC)
        user_id = record.get("user_id")
        decisions[alert_id] = Decision(
            alert_id=alert_id,
            kind=kind,
            decided_at=decided_at,
            user_id=user_id if isinstance(user_id, int) else None,
            outcome=DecisionOutcome.RECORDED,
        )
    return decisions


def _append_decision(
    *,
    directory: Path,
    alert_id: str,
    kind: DecisionKind,
    now: datetime,
    user_id: int | None,
    detail: str,
) -> None:
    payload = {
        "event": "decision",
        "alert_id": alert_id,
        "kind": kind.value,
        "decided_at": now.isoformat(),
        "user_id": user_id,
        "detail": detail,
    }
    _atomic_append(directory / DECISIONS_FILE, payload)


def request_approval(
    *,
    directory: Path,
    alert_id: str,
    user_id: int,
    now: datetime,
    ttl_seconds: int,
) -> ApprovalResult:
    """Enregistre une autorisation humaine, sous conditions vérifiées.

    Aucune décision n'est écrite si l'incident n'est pas ouvert, si le délai est
    dépassé ou si une décision existe déjà : un refus explicite vaut mieux qu'une
    autorisation silencieusement écrasée.
    """
    with _exclusive_lock(directory):
        try:
            if is_suspended(directory):
                return ApprovalResult(DecisionOutcome.WRONG_STATE, "maintenance suspendue")
            validate_authority(directory)
        except (OSError, ValueError):
            return ApprovalResult(DecisionOutcome.WRONG_STATE, "état de maintenance incertain")
        incidents = read_incidents(directory)
        incident = incidents.get(alert_id)
        if incident is None:
            return ApprovalResult(
                DecisionOutcome.WRONG_STATE, "incident inconnu pour cet identifiant"
            )
        if incident.status is not IncidentStatus.OPEN:
            return ApprovalResult(
                DecisionOutcome.WRONG_STATE,
                "incident déjà résolu : aucune autorisation n'est enregistrée",
                incident,
            )
        existing = read_decisions(directory).get(alert_id)
        if existing is not None:
            return ApprovalResult(
                DecisionOutcome.ALREADY_DECIDED,
                f"décision déjà enregistrée ({existing.kind.value})",
                incident,
            )
        deadline = incident.opened_at.timestamp() + ttl_seconds
        if now.timestamp() > deadline:
            _append_decision(
                directory=directory,
                alert_id=alert_id,
                kind=DecisionKind.EXPIRED,
                now=now,
                user_id=None,
                detail="demande expirée avant décision",
            )
            return ApprovalResult(
                DecisionOutcome.EXPIRED,
                "demande expirée : autorisation refusée, incident conservé ouvert",
                incident,
            )
        _append_decision(
            directory=directory,
            alert_id=alert_id,
            kind=DecisionKind.APPROVED,
            now=now,
            user_id=user_id,
            detail="autorisation d'analyse et de préparation, sans merge ni déploiement",
        )
    return ApprovalResult(
        DecisionOutcome.RECORDED,
        "autorisation enregistrée ; aucune correction n'est encore lancée",
        incident,
    )


def refuse_approval(
    *,
    directory: Path,
    alert_id: str,
    user_id: int,
    now: datetime,
) -> ApprovalResult:
    """Enregistre un refus explicite ; l'incident reste ouvert."""
    with _exclusive_lock(directory):
        incident = read_incidents(directory).get(alert_id)
        if incident is None:
            return ApprovalResult(
                DecisionOutcome.WRONG_STATE, "incident inconnu pour cet identifiant"
            )
        if incident.status is not IncidentStatus.OPEN:
            return ApprovalResult(
                DecisionOutcome.WRONG_STATE,
                "incident déjà résolu : aucune décision n'est enregistrée",
                incident,
            )
        existing = read_decisions(directory).get(alert_id)
        if existing is not None:
            return ApprovalResult(
                DecisionOutcome.ALREADY_DECIDED,
                f"décision déjà enregistrée ({existing.kind.value})",
                incident,
            )
        _append_decision(
            directory=directory,
            alert_id=alert_id,
            kind=DecisionKind.REFUSED,
            now=now,
            user_id=user_id,
            detail="refus explicite de préparation",
        )
    return ApprovalResult(
        DecisionOutcome.RECORDED, "refus enregistré ; incident conservé ouvert", incident
    )


def is_approved(directory: Path, alert_id: str) -> bool:
    """Indique si une autorisation exploitable existe pour cet incident."""
    decision = read_decisions(directory).get(alert_id)
    return decision is not None and decision.kind is DecisionKind.APPROVED


def expire_stale_requests(
    *, directory: Path, now: datetime, ttl_seconds: int
) -> list[str]:
    """Marque expirées les demandes restées sans décision au-delà du délai.

    Rend les identifiants expirés, pour que l'appelant puisse informer sans
    réécrire une décision déjà prise.
    """
    expired: list[str] = []
    with _exclusive_lock(directory):
        incidents = read_incidents(directory)
        decisions = read_decisions(directory)
        for alert_id, incident in incidents.items():
            if incident.status is not IncidentStatus.OPEN:
                continue
            if alert_id in decisions:
                continue
            if now.timestamp() <= incident.opened_at.timestamp() + ttl_seconds:
                continue
            _append_decision(
                directory=directory,
                alert_id=alert_id,
                kind=DecisionKind.EXPIRED,
                now=now,
                user_id=None,
                detail="demande expirée sans décision",
            )
            expired.append(alert_id)
    return expired


# ---------------------------------------------------------------------------
# Suspension
# ---------------------------------------------------------------------------


def suspend_maintenance(
    *, directory: Path, user_id: int | None, now: datetime, reason: str
) -> None:
    """Suspend la maintenance : seuls les nouveaux traitements s'arrêtent."""
    payload = {
        "suspended": True,
        "changed_at": now.isoformat(),
        "user_id": user_id,
        "reason": reason,
    }
    with _exclusive_lock(directory):
        _write_json_atomic(directory / SUSPENSION_FILE, payload)
        _atomic_append(
            directory / DECISIONS_FILE,
            {
                "event": "decision",
                "alert_id": "*",
                "kind": DecisionKind.SUSPENDED.value,
                "decided_at": now.isoformat(),
                "user_id": user_id,
                "detail": reason,
            },
        )


def resume_maintenance(
    *, directory: Path, user_id: int | None, now: datetime, reason: str
) -> None:
    """Reprend la maintenance sur décision explicite d'un opérateur."""
    payload = {
        "suspended": False,
        "changed_at": now.isoformat(),
        "user_id": user_id,
        "reason": reason,
    }
    with _exclusive_lock(directory):
        _write_json_atomic(directory / SUSPENSION_FILE, payload)
        _atomic_append(
            directory / DECISIONS_FILE,
            {
                "event": "decision",
                "alert_id": "*",
                "kind": DecisionKind.RESUMED.value,
                "decided_at": now.isoformat(),
                "user_id": user_id,
                "detail": reason,
            },
        )


def is_suspended(directory: Path) -> bool:
    """Indique si la maintenance est suspendue."""
    value = authoritative_object(directory / SUSPENSION_FILE)
    if value is not None and not isinstance(value.get("suspended"), bool):
        raise ValueError("suspension invalide")
    return bool(value and value["suspended"])


def suspension_state(directory: Path) -> dict[str, Any]:
    """Détail de la suspension courante, pour l'affichage opérateur."""
    return _read_json_object(directory / SUSPENSION_FILE)


# ---------------------------------------------------------------------------
# Contrôles impossibles
# ---------------------------------------------------------------------------


def read_technical_state(directory: Path) -> dict[str, Any]:
    return _read_json_object(directory / TECHNICAL_FILE)


def consecutive_failures(directory: Path) -> int:
    value = read_technical_state(directory).get("consecutive_failures")
    return value if isinstance(value, int) and value >= 0 else 0


def record_technical_failure(*, directory: Path, now: datetime, detail: str) -> int:
    """Incrémente le compteur d'échecs consécutifs et rend sa nouvelle valeur."""
    with _exclusive_lock(directory):
        current = consecutive_failures(directory)
        failures = current + 1
        _write_json_atomic(
            directory / TECHNICAL_FILE,
            {
                "consecutive_failures": failures,
                "last_failure_at": now.isoformat(),
                "last_detail": detail,
                "technical_alert_sent": bool(
                    read_technical_state(directory).get("technical_alert_sent")
                ),
            },
        )
    return failures


def mark_technical_alert_sent(*, directory: Path, now: datetime) -> None:
    with _exclusive_lock(directory):
        payload = read_technical_state(directory)
        payload["technical_alert_sent"] = True
        payload["technical_alert_at"] = now.isoformat()
        _write_json_atomic(directory / TECHNICAL_FILE, payload)


def technical_alert_sent(directory: Path) -> bool:
    return bool(read_technical_state(directory).get("technical_alert_sent"))


def reset_technical_failures(*, directory: Path) -> None:
    """Remet le compteur à zéro après un contrôle réussi."""
    with _exclusive_lock(directory):
        if not (directory / TECHNICAL_FILE).exists():
            return
        _write_json_atomic(
            directory / TECHNICAL_FILE,
            {
                "consecutive_failures": 0,
                "technical_alert_sent": False,
                "last_success_at": datetime.now(UTC).isoformat(),
            },
        )


def authoritative_object(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"état invalide : {path.name}")
    return value


def authoritative_records(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records = []
    for line in lines:
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"journal invalide : {path.name}")
            records.append(value)
    return records


def observe_incident(
    *, directory: Path, incident: bool, commit: str, severity: str, reason: str, now: datetime
) -> tuple[str, str | None]:
    with exclusive_lock(directory):
        if is_suspended(directory):
            return "suspended", None
        authoritative_records(directory / INCIDENTS_FILE)
        current = find_open_incident(directory)
        if current is not None and incident:
            return "continue", current.alert_id
        if current is None and not incident:
            return "none", None
        alert_id = current.alert_id if current else str(uuid.uuid4())
        payload = {"event": "incident", "alert_id": alert_id, "commit": commit}
        if incident:
            payload.update(
                status=IncidentStatus.OPEN.value,
                opened_at=now.isoformat(),
                severity=severity,
                reason=reason,
            )
        else:
            payload.update(status=IncidentStatus.VERIFIED.value, resolved_at=now.isoformat())
        _atomic_append(directory / INCIDENTS_FILE, payload)
        return ("open" if incident else "resolve"), alert_id


def reserve_notification(
    *,
    directory: Path,
    key: str,
    alert_id: str,
    observation_id: str,
    now: datetime,
    ttl_seconds: int,
) -> str | None:
    with exclusive_lock(directory):
        records = authoritative_records(directory / "notifications.jsonl")
        previous = next(
            (v for v in reversed(records) if v.get("key") == key and not v.get("late_result")), None
        )
        if previous:
            retry_at = parse_datetime(previous.get("retry_at"))
            if retry_at is None:
                raise ValueError("échéance de notification invalide")
            if now < retry_at:
                return None
            if previous.get("status") == "reserved":
                _atomic_append(
                    directory / "notifications.jsonl",
                    {
                        **previous,
                        "status": "interrupted",
                        "at": now.isoformat(),
                    },
                )
        reservation = uuid.uuid4().hex
        _atomic_append(
            directory / "notifications.jsonl",
            {
                "key": key,
                "alert_id": alert_id,
                "observation_id": observation_id,
                "reservation_id": reservation,
                "status": "reserved",
                "at": now.isoformat(),
                "retry_at": datetime.fromtimestamp(now.timestamp() + ttl_seconds, UTC).isoformat(),
            },
        )
        return reservation


def finish_notification(
    *, directory: Path, reservation_id: str, status: str, now: datetime
) -> None:
    if status not in {"accepted", "failed", "uncertain"}:
        raise ValueError("résultat de notification invalide")
    with exclusive_lock(directory):
        records = authoritative_records(directory / "notifications.jsonl")
        previous = next(
            (v for v in reversed(records) if v.get("reservation_id") == reservation_id), None
        )
        if previous is None or previous.get("status") not in {"reserved", "interrupted"}:
            raise ValueError("réservation de notification inactive")
        latest = next(
            (
                v
                for v in reversed(records)
                if v.get("key") == previous["key"] and not v.get("late_result")
            ),
            None,
        )
        payload = {
            **previous,
            "status": status,
            "at": now.isoformat(),
            "late_result": latest is not None and latest["reservation_id"] != reservation_id,
        }
        if status == "failed":
            payload["retry_at"] = datetime.fromtimestamp(now.timestamp() + 60, UTC).isoformat()
        _atomic_append(directory / "notifications.jsonl", payload)


def validate_authority(directory: Path) -> None:
    for name in (INCIDENTS_FILE, DECISIONS_FILE):
        for record in authoritative_records(directory / name):
            if not isinstance(record.get("alert_id"), str) or not record["alert_id"]:
                raise ValueError("identifiant d'autorité invalide")
            if name == INCIDENTS_FILE:
                if record.get("event") != "incident":
                    raise ValueError("événement d'incident invalide")
                status = IncidentStatus(str(record.get("status")))
                date_field = "resolved_at" if status == IncidentStatus.VERIFIED else "opened_at"
            else:
                if record.get("event") != "decision":
                    raise ValueError("événement de décision invalide")
                kind = DecisionKind(str(record.get("kind")))
                if (
                    kind in {DecisionKind.APPROVED, DecisionKind.REFUSED}
                    and type(record.get("user_id")) is not int
                ):
                    raise ValueError("décideur invalide")
                date_field = "decided_at"
            raw = record.get(date_field)
            if not isinstance(raw, str) or datetime.fromisoformat(raw).tzinfo is None:
                raise ValueError("horodatage d'autorité invalide")
