"""Modèles et artefacts du prototype de correction locale."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

SCHEMA_VERSION: Final = 1
MAX_FILES: Final = 3
MAX_LINES: Final = 120
MAX_BYTES: Final = 32 * 1024
MAX_PROPOSALS: Final = 2
MAX_DURATION_SECONDS: Final = 1800
MAX_TICKET_TTL: Final = timedelta(hours=4)
DEFAULT_STATE_DIR: Final = Path("~/.botradar/alert-fix")

_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
_DATETIME_FIELDS = frozenset({"issued_at", "expires_at"})
_TICKET_FIELDS = frozenset(
    {
        "schema_version",
        "ticket_id",
        "ticket_hash",
        "alert_id",
        "incident",
        "decision",
        "issued_at",
        "expires_at",
        "base_sha",
        "allowed_files",
        "test_file",
        "evidence_files",
        "trace",
        "limits",
    }
)
_INCIDENT_FIELDS = frozenset({"status", "opened_at", "commit", "severity", "reason"})
_DECISION_FIELDS = frozenset({"kind", "decided_at", "user_id"})
_LIMIT_FIELDS = frozenset(
    {
        "max_files",
        "max_lines",
        "max_bytes",
        "max_proposals",
        "max_duration_seconds",
    }
)
_TRACE_FIELDS = frozenset(
    {
        "ingested",
        "matches_created",
        "matches_notified",
        "blocked_actionable_due",
        "non_terminal_due",
        "sample",
        "timestamp",
        "commit",
        "severity",
        "reason",
        "window_hours",
        "stuck_hours",
    }
)
_SAMPLE_FIELDS = frozenset(
    {
        "match_id",
        "listing_id",
        "source_name",
        "notification_state",
        "attempt_count",
        "created_at",
        "state",
        "attempts",
    }
)


class AlertFixError(Exception):
    """Erreur contrôlée du prototype."""


class TicketError(AlertFixError):
    """Ticket absent, illisible ou non conforme."""


class TicketExpiredError(TicketError):
    """Ticket hors de sa fenêtre de validité."""


class TicketAlreadyUsedError(TicketError):
    """Ticket déjà consommé dans le ledger local."""


class ConfirmationError(TicketError):
    """Confirmation opérateur absente ou insuffisante."""


class PolicyError(AlertFixError):
    """Proposition hors politique, hors plafond ou fichier protégé."""


class ProposalError(AlertFixError):
    """Proposition enregistrée absente ou invalide."""


class AIUnavailableError(AlertFixError):
    """Appel IA refusé faute de mesure d'usage et de coût."""


class RunnerError(AlertFixError):
    """Exécution locale impossible ou interrompue."""


class Status(StrEnum):
    READY = "ready_for_review"
    OUT_OF_SCOPE = "out_of_scope"
    REPRODUCTION_FAILED = "reproduction_failed"
    FIX_FAILED = "fix_failed"
    CONTROL_FAILED = "control_failed"
    AI_ERROR = "ai_error"
    RUNNER_ERROR = "runner_error"
    TIMEOUT = "timeout"
    TICKET_INVALID = "ticket_invalid"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"
    WORKSPACE_MODIFIED = "workspace_modified"
    STARTED = "started"


def decision_for(status: Status) -> str:
    if status is Status.READY:
        return "prêt pour revue"
    if status is Status.OUT_OF_SCOPE:
        return "hors périmètre"
    return "échec"


def exit_code_for(status: Status) -> int:
    if status is Status.READY:
        return 0
    if status is Status.TICKET_INVALID:
        return 2
    if status is Status.OUT_OF_SCOPE:
        return 3
    if status in {Status.EXPIRED, Status.ALREADY_USED}:
        return 4
    if status is Status.AI_ERROR:
        return 5
    return 1


def default_state_dir() -> Path:
    return DEFAULT_STATE_DIR.expanduser()


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_datetime(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise TicketError(f"{field} doit être une chaîne ISO-8601")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TicketError(f"{field} n'est pas une date ISO-8601") from exc
    if parsed.tzinfo is None:
        raise TicketError(f"{field} doit porter un fuseau horaire")
    return parsed.astimezone(UTC)


def _require_dict(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TicketError(f"{field} doit être un objet JSON")
    return value


def _check_keys(value: Mapping[str, Any], allowed: frozenset[str], *, field: str) -> None:
    unknown = set(value) - set(allowed)
    if unknown:
        raise TicketError(f"{field} contient des champs inconnus : {sorted(unknown)}")


def _require_str(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TicketError(f"{field} doit être une chaîne non vide")
    return value


def _require_int(value: Any, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise TicketError(f"{field} doit être un entier positif")
    return value


def _require_str_list(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise TicketError(f"{field} doit être une liste non vide")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise TicketError(f"{field} ne doit contenir que des chaînes non vides")
    return tuple(value)


@dataclass(frozen=True)
class Limits:
    max_files: int = MAX_FILES
    max_lines: int = MAX_LINES
    max_bytes: int = MAX_BYTES
    max_proposals: int = MAX_PROPOSALS
    max_duration_seconds: int = MAX_DURATION_SECONDS

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Limits:
        _check_keys(payload, _LIMIT_FIELDS, field="limits")
        values = {
            name: _require_int(payload.get(name), field=f"limits.{name}")
            for name in _LIMIT_FIELDS
        }
        return cls(**values)

    def lowered(self, ceiling: Limits) -> Limits:
        return Limits(
            max_files=min(self.max_files, ceiling.max_files),
            max_lines=min(self.max_lines, ceiling.max_lines),
            max_bytes=min(self.max_bytes, ceiling.max_bytes),
            max_proposals=min(self.max_proposals, ceiling.max_proposals),
            max_duration_seconds=min(
                self.max_duration_seconds, ceiling.max_duration_seconds
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_files": self.max_files,
            "max_lines": self.max_lines,
            "max_bytes": self.max_bytes,
            "max_proposals": self.max_proposals,
            "max_duration_seconds": self.max_duration_seconds,
        }


@dataclass(frozen=True)
class Incident:
    status: str
    opened_at: datetime
    commit: str
    severity: str
    reason: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Incident:
        _check_keys(payload, _INCIDENT_FIELDS, field="incident")
        return cls(
            status=_require_str(payload.get("status"), field="incident.status"),
            opened_at=parse_datetime(payload.get("opened_at"), field="incident.opened_at"),
            commit=_require_str(payload.get("commit"), field="incident.commit"),
            severity=_require_str(payload.get("severity"), field="incident.severity"),
            reason=_require_str(payload.get("reason"), field="incident.reason"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "opened_at": self.opened_at.isoformat(),
            "commit": self.commit,
            "severity": self.severity,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Decision:
    kind: str
    decided_at: datetime
    user_id: int

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Decision:
        _check_keys(payload, _DECISION_FIELDS, field="decision")
        user_id = payload.get("user_id")
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise TicketError("decision.user_id doit être un entier")
        return cls(
            kind=_require_str(payload.get("kind"), field="decision.kind"),
            decided_at=parse_datetime(payload.get("decided_at"), field="decision.decided_at"),
            user_id=user_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "decided_at": self.decided_at.isoformat(),
            "user_id": self.user_id,
        }


@dataclass(frozen=True)
class Ticket:
    schema_version: int
    ticket_id: str
    ticket_hash: str
    alert_id: str
    incident: Incident
    decision: Decision
    issued_at: datetime
    expires_at: datetime
    base_sha: str
    allowed_files: tuple[str, ...]
    test_file: str
    evidence_files: tuple[str, ...]
    trace: dict[str, Any]
    limits: Limits

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Ticket:
        _check_keys(payload, _TICKET_FIELDS, field="ticket")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise TicketError("schema_version non pris en charge")
        ticket_id = _require_str(payload.get("ticket_id"), field="ticket_id")
        ticket_hash = _require_str(payload.get("ticket_hash"), field="ticket_hash")
        alert_id = _require_str(payload.get("alert_id"), field="alert_id")
        incident = Incident.from_dict(_require_dict(payload.get("incident"), field="incident"))
        decision = Decision.from_dict(_require_dict(payload.get("decision"), field="decision"))
        issued_at = parse_datetime(payload.get("issued_at"), field="issued_at")
        expires_at = parse_datetime(payload.get("expires_at"), field="expires_at")
        base_sha = _require_str(payload.get("base_sha"), field="base_sha")
        if not _SHA_PATTERN.fullmatch(base_sha):
            raise TicketError("base_sha doit être un SHA hexadécimal")
        allowed_files = _require_str_list(payload.get("allowed_files"), field="allowed_files")
        evidence_files = _require_str_list(payload.get("evidence_files"), field="evidence_files")
        test_file = _require_str(payload.get("test_file"), field="test_file")
        trace = dict(_require_dict(payload.get("trace"), field="trace"))
        _check_keys(trace, _TRACE_FIELDS, field="trace")
        sample = trace.get("sample", [])
        if not isinstance(sample, list) or len(sample) > 8:
            raise TicketError("trace.sample doit être une liste d'au plus 8 éléments")
        for item in sample:
            if not isinstance(item, dict):
                raise TicketError("trace.sample doit contenir des objets")
            _check_keys(item, _SAMPLE_FIELDS, field="trace.sample[]")
        if len(set(allowed_files)) != len(allowed_files):
            raise TicketError("allowed_files contient des doublons")
        if len(allowed_files) > MAX_FILES:
            raise TicketError("allowed_files dépasse le plafond dur")
        if test_file not in allowed_files:
            raise TicketError("test_file doit figurer dans allowed_files")
        if not set(evidence_files).issubset(set(allowed_files)):
            raise TicketError("evidence_files doit être un sous-ensemble de allowed_files")
        if expires_at <= issued_at:
            raise TicketError("expires_at doit être postérieur à issued_at")
        if expires_at - issued_at > MAX_TICKET_TTL:
            raise TicketError("la fenêtre du ticket dépasse 4 heures")
        if incident.status != "ouvert":
            raise TicketError("le ticket ne vise pas un incident ouvert")
        if decision.kind != "approuvé":
            raise TicketError("le ticket ne porte pas une décision approuvée")
        return cls(
            schema_version=SCHEMA_VERSION,
            ticket_id=ticket_id,
            ticket_hash=ticket_hash,
            alert_id=alert_id,
            incident=incident,
            decision=decision,
            issued_at=issued_at,
            expires_at=expires_at,
            base_sha=base_sha,
            allowed_files=allowed_files,
            test_file=test_file,
            evidence_files=evidence_files,
            trace=trace,
            limits=Limits.from_dict(_require_dict(payload.get("limits"), field="limits")),
        )

    def unsigned(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ticket_id": self.ticket_id,
            "alert_id": self.alert_id,
            "incident": self.incident.to_dict(),
            "decision": self.decision.to_dict(),
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "base_sha": self.base_sha,
            "allowed_files": list(self.allowed_files),
            "test_file": self.test_file,
            "evidence_files": list(self.evidence_files),
            "trace": self.trace,
            "limits": self.limits.to_dict(),
        }

    def compute_hash(self) -> str:
        return sha256_text(canonical_json(self.unsigned()))

    def to_dict(self) -> dict[str, Any]:
        payload = self.unsigned()
        payload["ticket_hash"] = self.ticket_hash
        return payload


def load_ticket(path: Path, *, now: datetime | None = None) -> Ticket:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TicketError("ticket introuvable") from exc
    except (OSError, ValueError) as exc:
        raise TicketError("ticket illisible") from exc
    ticket = Ticket.from_dict(_require_dict(payload, field="ticket"))
    expected = ticket.compute_hash()
    if expected != ticket.ticket_hash:
        raise TicketError("hash du ticket invalide")
    if moment > ticket.expires_at:
        raise TicketExpiredError("ticket expiré")
    if ticket.issued_at > moment + timedelta(minutes=5):
        raise TicketError("ticket émis dans le futur")
    return ticket


def effective_limits(ticket: Ticket) -> Limits:
    return ticket.limits.lowered(Limits())


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def append_ledger(*, state_dir: Path, ticket: Ticket, run_id: str, now: datetime) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    ledger = state_dir / "used_tickets.jsonl"
    lock_path = state_dir / ".ledger.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if ledger.exists():
                for line in ledger.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        used = json.loads(line)
                    except ValueError as exc:
                        raise TicketError("ledger local illisible") from exc
                    if not isinstance(used, dict):
                        raise TicketError("ledger local illisible")
                    if used.get("ticket_id") == ticket.ticket_id:
                        raise TicketAlreadyUsedError("ticket déjà utilisé")
                    if used.get("ticket_hash") == ticket.ticket_hash:
                        raise TicketAlreadyUsedError("ticket déjà utilisé")
            entry = {
                "ticket_id": ticket.ticket_id,
                "ticket_hash": ticket.ticket_hash,
                "run_id": run_id,
                "used_at": now.isoformat(),
            }
            with ledger.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def check_suspension(state_dir: Path) -> tuple[bool, bool]:
    path = state_dir / "suspension.json"
    if not path.exists():
        return False, False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TicketError("état de suspension illisible") from exc
    if not isinstance(payload, dict):
        raise TicketError("état de suspension illisible")
    suspended = payload.get("suspended") is True
    if suspended:
        raise PolicyError("maintenance suspendue")
    return False, True


@dataclass(frozen=True)
class CommandOutcome:
    name: str
    argv: tuple[str, ...]
    exit_code: int
    duration_seconds: float
    stdout: str
    stderr: str
    timed_out: bool = False
    workspace_modified: bool = False
    limits_applied: tuple[str, ...] = ()
    infra_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class TestObservation:
    collected: int
    failed: int
    passed: int
    errors: int
    exit_code: int
    functional_failure: bool
    report_valid: bool
    nodeids: tuple[str, ...]
    output_excerpt: str


@dataclass(frozen=True)
class DiffStats:
    files: tuple[str, ...]
    added: int
    removed: int
    bytes: int
    sha256: str


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TicketError("fichier JSON illisible") from exc
    return _require_dict(payload, field=path.name)


def sorted_used_tickets(state_dir: Path) -> list[dict[str, Any]]:
    ledger = state_dir / "used_tickets.jsonl"
    if not ledger.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            raise TicketError("ledger local illisible")
        entries.append(parsed)
    return entries


def bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n[... tronqué ...]"


def redact_text(value: str) -> str:
    patterns: Sequence[tuple[re.Pattern[str], str]] = (
        (re.compile(r"https?://\S+", re.IGNORECASE), "[url]"),
        (re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "[email]"),
        (
            re.compile(
                r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                r"sk-[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
            ),
            "[token]",
        ),
        (
            re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
            "[autorisation]",
        ),
        (
            re.compile(
                r"(?i)\b(?:DATABASE_URL|TELEGRAM_BOT_TOKEN|AI_API_KEY|GH_TOKEN|"
                r"GITHUB_TOKEN|SSH_AUTH_SOCK|DOCKER_HOST|PASSWORD|SECRET)\b"
                r"\s*[=:]\s*\S+"
            ),
            "[secret]",
        ),
        (
            re.compile(
                r"(?<![\w.])/(?:Users|home|private/var|var/folders|tmp|opt|srv|usr/local)/"
                r"[^\s\"'<>]+"
            ),
            "[chemin]",
        ),
        (
            re.compile(
                r"(?:\.\./)+(?:Users|home|private/var|var/folders|tmp|opt|srv|usr/local)/"
                r"[^\s\"'<>]+"
            ),
            "[chemin]",
        ),
    )
    redacted = value
    for pattern, replacement in patterns:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def excerpt(value: str, limit: int = 4096) -> str:
    return bounded_text(redact_text(value.replace("\x00", "")), limit)
