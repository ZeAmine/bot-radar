#!/usr/bin/env python3
"""Détecte une stagnation entre l'ingestion d'annonces et les notifications Telegram.

Contrôle en **lecture seule** : ce script n'écrit jamais en base, n'appelle aucun
moteur IA et ne corrige rien. Il observe une fenêtre glissante et ne déclare un
incident critique que lorsqu'une correspondance attend réellement un traitement
dont le délai admissible est dépassé. La simple création de correspondances, une
correspondance fraîche ou une reprise prévue dans le futur ne déclenchent rien.

La clôture suit le **symptôme**, pas le correctif : une alerte reste ouverte tant
que le même détecteur la reproduit. Elle n'est marquée « vérifiée » que lorsqu'un
relevé ultérieur ne la reproduit plus. Un correctif proposé, fusionné ou déployé
ne clôt jamais une alerte par lui-même.

Usage :
    python scripts/check_alert_chain.py
    python scripts/check_alert_chain.py --dry-run
    python scripts/check_alert_chain.py --window-hours 2 --stuck-hours 1

Codes de sortie : 0 aucun incident, 1 incident détecté, 2 contrôle impossible.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import setup_logging
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.domain.enums import NotificationState

logger = logging.getLogger("radar.alert_chain")

EXIT_OK, EXIT_INCIDENT, EXIT_ERROR = 0, 1, 2

DEFAULT_WINDOW_HOURS = 2.0
DEFAULT_STUCK_HOURS = 1.0
DEFAULT_MAX_SAMPLE = 8
DEFAULT_DEDUP_SECONDS = 3600

INCIDENT_DEDUP_KEY = "radar:alert_chain:incident:notified"
RESOLUTION_DEDUP_KEY = "radar:alert_chain:resolved:notified"

STATUS_OPEN = "ouvert"
STATUS_VERIFIED = "vérifié"
STATUS_OK = "ok"

# États qu'une reprise F03-B doit faire progresser. `notified` et `abandoned` sont
# terminaux. `unknown` n'autorise aucun nouvel envoi, mais un `unknown` porteur d'un
# reçu exploitable reste actionnable pour sa finalisation (marquage SQL).
ACTIONABLE_STATES = (
    NotificationState.READY.value,
    NotificationState.DEFERRED.value,
    NotificationState.SENDING.value,
)

SendMessage = Callable[[str], Awaitable[None]]
ClaimSlot = Callable[[str, int], Awaitable[bool]]

_dedup_deadlines: dict[str, float] = {}


@dataclass(frozen=True)
class BlockedMatch:
    """Correspondance bloquée, réduite aux identifiants nécessaires au diagnostic."""

    match_id: str
    listing_id: str
    source_name: str
    notification_state: str
    attempt_count: int | None
    created_at: datetime


@dataclass(frozen=True)
class Snapshot:
    """Relevé d'une fenêtre : compteurs et échantillon de correspondances bloquées."""

    window_start: datetime
    stuck_before: datetime
    window_hours: float
    stuck_hours: float
    ingested: int
    acknowledged: int
    matches_created: int
    matches_notified: int
    blocked_actionable_due: int
    non_terminal_due: int
    blocked_sample: tuple[BlockedMatch, ...]


@dataclass(frozen=True)
class Verdict:
    """Décision déterministe : incident ou non, gravité et motif lisible."""

    incident: bool
    severity: str
    reason: str


def evaluate(snapshot: Snapshot) -> Verdict:
    """Qualifie un relevé sans aucun accès externe.

    Un incident critique exige une correspondance réellement en attente de
    traitement, dont le délai admissible est dépassé : actionnable, échue et plus
    ancienne que la garde d'âge. La simple création de correspondances, une
    correspondance fraîche ou une reprise future ne déclenchent rien.
    """
    if snapshot.blocked_actionable_due > 0:
        return Verdict(True, "critical", "correspondances actionnables en retard")
    if snapshot.matches_created == 0:
        return Verdict(
            False, "ok", "aucune correspondance créée sur la fenêtre ; aucune notification attendue"
        )
    return Verdict(
        False, "ok", "aucune correspondance actionnable échue ; aucun incident détecté"
    )


def decide(*, incident: bool, open_alert_id: str | None) -> str:
    """Choisit l'action de suivi : ouvrir, poursuivre, clore ou ne rien faire."""
    if incident and open_alert_id is None:
        return "open"
    if incident:
        return "continue"
    if open_alert_id is not None:
        return "resolve"
    return "none"


def _blocked_filter(*, now: datetime, stuck_before: datetime) -> Any:
    finalizable_receipt = and_(
        ListingMatch.notification_state == NotificationState.UNKNOWN.value,
        ListingMatch.telegram_message_id > 0,
        ListingMatch.telegram_chat_id != 0,
        ListingMatch.telegram_bot_id > 0,
        ListingMatch.attempt_id.is_not(None),
    )
    return and_(
        ListingMatch.notified_at.is_(None),
        ListingMatch.created_at < stuck_before,
        or_(ListingMatch.notification_state.in_(ACTIONABLE_STATES), finalizable_receipt),
        or_(ListingMatch.next_attempt_at.is_(None), ListingMatch.next_attempt_at <= now),
    )


def _non_terminal_filter(*, now: datetime) -> Any:
    return and_(
        ListingMatch.notified_at.is_(None),
        ListingMatch.notification_state.not_in(
            (NotificationState.NOTIFIED.value, NotificationState.ABANDONED.value)
        ),
        or_(ListingMatch.next_attempt_at.is_(None), ListingMatch.next_attempt_at <= now),
    )


async def collect_snapshot(
    session: AsyncSession,
    *,
    now: datetime,
    window_hours: float,
    stuck_hours: float,
    max_sample: int,
) -> Snapshot:
    """Lit les annonces, les correspondances et les blocages actionnables."""
    window_start = now - timedelta(hours=window_hours)
    stuck_before = now - timedelta(hours=stuck_hours)

    ingested = (
        await session.scalar(
            select(func.count()).select_from(Listing).where(Listing.received_at >= window_start)
        )
        or 0
    )
    matches_created = (
        await session.scalar(
            select(func.count())
            .select_from(ListingMatch)
            .where(ListingMatch.created_at >= window_start)
        )
        or 0
    )
    matches_notified = (
        await session.scalar(
            select(func.count())
            .select_from(ListingMatch)
            .where(
                ListingMatch.notified_at.is_not(None),
                ListingMatch.notified_at >= window_start,
            )
        )
        or 0
    )

    blocked_filter = _blocked_filter(now=now, stuck_before=stuck_before)
    blocked_actionable_due = (
        await session.scalar(
            select(func.count()).select_from(ListingMatch).where(blocked_filter)
        )
        or 0
    )
    non_terminal_due = (
        await session.scalar(
            select(func.count())
            .select_from(ListingMatch)
            .where(_non_terminal_filter(now=now))
        )
        or 0
    )
    rows = (
        await session.execute(
            select(ListingMatch, Listing)
            .join(Listing, ListingMatch.listing_id == Listing.id)
            .where(blocked_filter)
            .order_by(ListingMatch.created_at.asc(), ListingMatch.id.asc())
            .limit(max_sample)
        )
    ).all()
    sample = tuple(
        BlockedMatch(
            match_id=str(match.id),
            listing_id=str(match.listing_id),
            source_name=listing.source_name,
            notification_state=str(match.notification_state),
            attempt_count=match.attempt_count,
            created_at=match.created_at,
        )
        for match, listing in rows
    )
    return Snapshot(
        window_start=window_start,
        stuck_before=stuck_before,
        window_hours=window_hours,
        stuck_hours=stuck_hours,
        ingested=int(ingested),
        acknowledged=int(matches_notified),
        matches_created=int(matches_created),
        matches_notified=int(matches_notified),
        blocked_actionable_due=int(blocked_actionable_due),
        non_terminal_due=int(non_terminal_due),
        blocked_sample=sample,
    )


def _short(value: str) -> str:
    return value[:8]


def format_alert(
    snapshot: Snapshot,
    verdict: Verdict,
    *,
    alert_id: str,
    commit: str,
    now: datetime,
) -> str:
    """Message Telegram : preuve, gravité et identifiants, sans titre ni URL."""
    head = "🚨" if verdict.severity == "critical" else "⚠️"
    lines = [
        f"{head} <b>Chaîne d'alerte : incident</b>",
        "",
        f"<b>Gravité :</b> {escape(verdict.severity)}",
        "<b>Fenêtre :</b> "
        f"{snapshot.window_hours:g} h depuis {snapshot.window_start.isoformat(timespec='minutes')}",
        f"<b>Motif :</b> {escape(verdict.reason)}",
        f"<b>Annonces ingérées :</b> {snapshot.ingested}",
        f"<b>Notifications acquittées :</b> {snapshot.acknowledged}",
        f"<b>Correspondances créées :</b> {snapshot.matches_created}",
        f"<b>Correspondances notifiées :</b> {snapshot.matches_notified}",
        f"<b>Correspondances actionnables en retard :</b> {snapshot.blocked_actionable_due}",
        f"<b>Correspondances non terminales échues :</b> {snapshot.non_terminal_due}",
    ]
    if snapshot.blocked_sample:
        lines += ["", "<b>Échantillon bloqué :</b>"]
        lines += [
            f"• <code>{_short(item.match_id)}</code> {escape(item.source_name)} "
            f"— état {escape(item.notification_state)}, tentatives {item.attempt_count or 0}"
            for item in snapshot.blocked_sample
        ]
    lines += [
        "",
        f"<b>Horodatage :</b> {now.isoformat(timespec='seconds')}",
        f"<b>Commit main analysé :</b> <code>{escape(commit)}</code>",
        f"<b>Alerte :</b> <code>{alert_id}</code>",
        "<i>Vérifier les journaux du conteneur radar_app puis la reprise F03-B.</i>",
    ]
    return "\n".join(lines)


def format_resolution(
    snapshot: Snapshot,
    *,
    alert_id: str,
    commit: str,
    now: datetime,
) -> str:
    """Message de clôture, émis seulement après un relevé qui ne reproduit plus."""
    return "\n".join(
        [
            "✅ <b>Chaîne d'alerte rétablie</b>",
            "",
            f"Le symptôme de l'alerte <code>{alert_id}</code> n'est plus reproduit : "
            "des annonces récentes sont acquittées ou aucun blocage ne persiste.",
            f"<b>Annonces ingérées :</b> {snapshot.ingested}",
            f"<b>Notifications acquittées :</b> {snapshot.acknowledged}",
            f"<b>Correspondances créées :</b> {snapshot.matches_created}",
            f"<b>Correspondances notifiées :</b> {snapshot.matches_notified}",
            f"<b>Correspondances actionnables en retard :</b> {snapshot.blocked_actionable_due}",
            f"<b>Correspondances non terminales échues :</b> {snapshot.non_terminal_due}",
            "",
            f"<b>Horodatage :</b> {now.isoformat(timespec='seconds')}",
            f"<b>Commit main analysé :</b> <code>{escape(commit)}</code>",
            "<b>Statut :</b> vérifié (rejeu du même contrôle après livraison)",
        ]
    )


def _sample_as_dicts(sample: Iterable[BlockedMatch]) -> list[dict[str, Any]]:
    return [
        {
            "match_id": item.match_id,
            "listing_id": item.listing_id,
            "source_name": item.source_name,
            "notification_state": item.notification_state,
            "attempt_count": item.attempt_count,
            "created_at": item.created_at.isoformat(),
        }
        for item in sample
    ]


def build_record(
    *,
    now: datetime,
    status: str,
    action: str,
    alert_id: str | None,
    commit: str,
    snapshot: Snapshot,
    verdict: Verdict,
    resolves: str | None = None,
) -> dict[str, Any]:
    """Trace persistée : seuils, requête, résultat et statut d'une observation."""
    record: dict[str, Any] = {
        "event": "alert_chain",
        "status": status,
        "action": action,
        "alert_id": alert_id,
        "timestamp": now.isoformat(),
        "commit": commit,
        "thresholds": {
            "window_hours": snapshot.window_hours,
            "stuck_hours": snapshot.stuck_hours,
        },
        "results": {
            "ingested": snapshot.ingested,
            "acknowledged": snapshot.acknowledged,
            "matches_created": snapshot.matches_created,
            "matches_notified": snapshot.matches_notified,
            "blocked_actionable_due": snapshot.blocked_actionable_due,
            "non_terminal_due": snapshot.non_terminal_due,
            "severity": verdict.severity,
            "reason": verdict.reason,
            "blocked_sample": _sample_as_dicts(snapshot.blocked_sample),
        },
    }
    if resolves is not None:
        record["resolves"] = resolves
    return record


def read_records(path: Path) -> list[dict[str, Any]]:
    """Relit la trace JSONL ; une ligne illisible est ignorée, jamais fatale."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            logger.warning("Trace illisible ignorée dans %s", path)
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def find_open_alert(records: Iterable[dict[str, Any]]) -> str | None:
    """Rend l'alerte non close la plus récente, ou `None` si le symptôme a disparu."""
    open_ids: dict[str, None] = {}
    for record in records:
        if record.get("event") != "alert_chain":
            continue
        status = record.get("status")
        alert_id = record.get("alert_id")
        if status == STATUS_OPEN and isinstance(alert_id, str):
            open_ids[alert_id] = None
        elif status == STATUS_VERIFIED:
            resolved = record.get("resolves")
            if isinstance(resolved, str):
                open_ids.pop(resolved, None)
    if not open_ids:
        return None
    return next(reversed(open_ids))


async def _claim_notification_slot(key: str, ttl_seconds: int) -> bool:
    """Dédoublonne une notification ; repli local si Redis est indisponible."""
    now = monotonic()
    if _dedup_deadlines.get(key, 0.0) > now:
        return False
    try:
        from app.core.redis import get_redis_client

        claimed = bool(await get_redis_client().set(key, "1", ex=ttl_seconds, nx=True))
    except Exception:
        logger.warning("Dédoublonnage Redis indisponible ; repli local pour %s", key)
        claimed = True
    if claimed:
        _dedup_deadlines[key] = now + ttl_seconds
    return claimed


async def _send_via_admins(text: str) -> None:
    from app.services.telegram_service import notify_admins

    await notify_admins(text)


async def run_check(
    *,
    session: AsyncSession,
    state_file: Path | None,
    window_hours: float,
    stuck_hours: float,
    max_sample: int,
    dedup_seconds: int,
    commit: str,
    now: datetime,
    send: SendMessage = _send_via_admins,
    claim: ClaimSlot = _claim_notification_slot,
    dry_run: bool = False,
) -> int:
    """Exécute un contrôle, met à jour la trace et notifie si nécessaire."""
    records = read_records(state_file) if state_file is not None else []
    open_alert_id = find_open_alert(records)

    snapshot = await collect_snapshot(
        session,
        now=now,
        window_hours=window_hours,
        stuck_hours=stuck_hours,
        max_sample=max_sample,
    )
    verdict = evaluate(snapshot)
    action = decide(incident=verdict.incident, open_alert_id=open_alert_id)

    alert_id = open_alert_id
    status = STATUS_OK
    message: str | None = None
    resolves: str | None = None

    if action == "open":
        alert_id = str(uuid.uuid4())
        status = STATUS_OPEN
        message = format_alert(snapshot, verdict, alert_id=alert_id, commit=commit, now=now)
    elif action == "continue":
        status = STATUS_OPEN
        if await claim(INCIDENT_DEDUP_KEY, dedup_seconds):
            message = format_alert(
                snapshot, verdict, alert_id=alert_id or "", commit=commit, now=now
            )
    elif action == "resolve":
        status = STATUS_VERIFIED
        resolves = alert_id
        message = format_resolution(snapshot, alert_id=alert_id or "", commit=commit, now=now)
        if not await claim(RESOLUTION_DEDUP_KEY, dedup_seconds):
            message = None

    record = build_record(
        now=now,
        status=status,
        action=action,
        alert_id=alert_id,
        commit=commit,
        snapshot=snapshot,
        verdict=verdict,
        resolves=resolves,
    )

    logger.info(
        "Contrôle chaîne d'alerte : %s",
        verdict.reason,
        extra={
            "event": "alert_chain_check",
            "status": status,
            "action": action,
            "alert_id": alert_id,
            "severity": verdict.severity,
            "ingested": snapshot.ingested,
            "acknowledged": snapshot.acknowledged,
            "matches_created": snapshot.matches_created,
            "matches_notified": snapshot.matches_notified,
            "blocked_actionable_due": snapshot.blocked_actionable_due,
            "non_terminal_due": snapshot.non_terminal_due,
            "window_hours": snapshot.window_hours,
            "stuck_hours": snapshot.stuck_hours,
            "commit": commit,
        },
    )

    if dry_run:
        print(json.dumps(record, ensure_ascii=False))
        if message:
            print(message)
    else:
        if state_file is not None:
            append_record(state_file, record)
        if message:
            await send(message)

    return EXIT_INCIDENT if verdict.incident else EXIT_OK


def _resolve_state_file(args: argparse.Namespace) -> Path | None:
    if args.no_state:
        return None
    if args.state_file:
        return Path(args.state_file).expanduser()
    configured = os.environ.get("ALERT_CHAIN_STATE_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "botradar" / "alert-chain.jsonl"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--stuck-hours", type=float, default=DEFAULT_STUCK_HOURS)
    parser.add_argument("--max-sample", type=int, default=DEFAULT_MAX_SAMPLE)
    parser.add_argument("--dedup-seconds", type=int, default=DEFAULT_DEDUP_SECONDS)
    parser.add_argument("--commit", default=os.environ.get("GIT_COMMIT", "inconnu"))
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--no-state", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche le relevé et le message sans écrire la trace ni notifier",
    )
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        return await run_check(
            session=session,
            state_file=_resolve_state_file(args),
            window_hours=args.window_hours,
            stuck_hours=args.stuck_hours,
            max_sample=args.max_sample,
            dedup_seconds=args.dedup_seconds,
            commit=args.commit,
            now=datetime.now(UTC),
            dry_run=args.dry_run,
        )


def main(argv: list[str] | None = None) -> int:
    setup_logging("INFO")
    args = _parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        return EXIT_ERROR
    except Exception as exc:
        logger.error("Contrôle impossible : %s", type(exc).__name__)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
