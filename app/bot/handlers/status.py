"""Handler pour la commande /status."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.config import settings
from app.db.repositories.stats_repo import StatsRepository
from app.providers.llm.registry import llm_client
from app.tasks.leboncoin_poller import get_poller_status as get_leboncoin_poller_status
from app.tasks.vinted_poller import get_poller_status, is_night_time

router = Router(name="status_router")


def _leboncoin_status_line(data: dict[str, Any] | None, now: datetime) -> str:
    """État de la collecte Leboncoin : sans elle, une sortie résidentielle tombée est muette."""
    if not settings.LEBONCOIN_POLLER_ENABLED:
        return "⏸️ <b>Désactivé</b>"
    if not data:
        return "⚪ <b>Inconnu</b> (aucun état exploitable)"
    status = data.get("status")
    if status == "cooldown":
        cooldown = data.get("cooldown_seconds")
        minutes = int(cooldown) // 60 if isinstance(cooldown, (int, float)) else 0
        return f"🛡️ <b>Pause anti-blocage</b> ({minutes} min)"
    if status == "active":
        last_poll = data.get("last_poll_at")
        if isinstance(last_poll, bool) or not isinstance(last_poll, (int, float)):
            return "⚪ <b>Inconnu</b> (horodatage absent)"
        elapsed = int(max(0.0, now.timestamp() - last_poll))
        if elapsed <= settings.LEBONCOIN_POLL_INTERVAL_SECONDS * 3:
            since = data.get("ads_since_last_cycle")
            suffix = (
                f" · {since} nouvelles"
                if isinstance(since, int) and not isinstance(since, bool)
                else ""
            )
            return f"🟢 <b>Active</b> (collecte -{elapsed}s{suffix})"
        return f"🟠 <b>En retard</b> ({elapsed // 60} min)"
    if status == "stopped":
        return "🔴 <b>Arrêté</b>"
    return "⚪ <b>Inconnu</b> (aucun état exploitable)"


def _format_span(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes:02d}"
    if minutes:
        return f"{minutes} min {secs:02d}s" if secs else f"{minutes} min"
    return f"{secs} s"


def _availability_line(poller_data: dict[str, Any] | None) -> str:
    availability = (poller_data or {}).get("availability") or {}
    window_s = availability.get("window_s")
    if isinstance(window_s, bool) or not isinstance(window_s, (int, float)) or window_s <= 0:
        return ""
    scanned = availability.get("scanned_s")
    scanned_s = (
        float(scanned)
        if isinstance(scanned, (int, float)) and not isinstance(scanned, bool)
        else 0.0
    )
    share = round(100 * scanned_s / float(window_s))
    line = f"<b>Disponibilité scan :</b> {share} % ({_format_span(float(window_s))})"
    details = []
    for key, label in (
        ("blocked_s", "bloqué"),
        ("night_s", "nuit"),
        ("network_error_s", "réseau"),
        ("unexpected_error_s", "erreurs"),
    ):
        value = availability.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 1:
            continue
        details.append(f"{label} {_format_span(float(value))}")
    if details:
        line += " · " + " · ".join(details)
    return line


def _searches_line(poller_data: dict[str, Any] | None) -> str:
    stats = (poller_data or {}).get("search_stats") or {}
    count = stats.get("count")
    worst = stats.get("worst_age_s")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        return ""
    if isinstance(worst, bool) or not isinstance(worst, (int, float)) or worst < 0:
        return ""
    return f"<b>Recherches perso :</b> {count} · plus ancien scan {_format_span(float(worst))}"


def _requests_line(poller_data: dict[str, Any] | None) -> str:
    counts = (poller_data or {}).get("requests_60s") or {}
    parts = []
    for key, label in (("api", "API"), ("html", "HTML"), ("mint", "mint")):
        value = counts.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        parts.append(f"{label} {int(value)}")
    if not parts:
        return ""
    return "<b>Requêtes Vinted (60 s) :</b> " + " · ".join(parts)


@router.message(Command("status"))
async def handle_status(message: Message, db_session: Any) -> None:
    """Affiche l'état opérationnel en direct de BotRadar."""
    tz_label = settings.NIGHT_MODE_TIMEZONE or "Europe/Paris"
    try:
        tz = ZoneInfo(tz_label)
    except Exception:
        tz = ZoneInfo("Europe/Paris")
        tz_label = "Europe/Paris"
    now = datetime.now(tz)
    time_str = now.strftime("%H:%M")

    night_active = is_night_time(now)
    poller_data = await get_poller_status()
    leboncoin_data = (
        await get_leboncoin_poller_status() if settings.LEBONCOIN_POLLER_ENABLED else None
    )
    leboncoin_status = _leboncoin_status_line(leboncoin_data, now)

    if night_active:
        poller_status = "<b>Mode nuit</b>"
    elif not settings.VINTED_POLLER_ENABLED:
        poller_status = "⏸️ <b>Désactivé</b>"
    elif poller_data and poller_data.get("status") == "cooldown":
        cooldown_until = poller_data.get("cooldown_until") or 0
        rem_sec = max(0, int(cooldown_until - now.timestamp()))
        consecutive = poller_data.get("consecutive_blocks", 1)
        reason = poller_data.get("reason", "Blocage Vinted (403)")
        resume_time = poller_data.get("resume_time") or (
            datetime.fromtimestamp(cooldown_until, tz).strftime("%H:%M") if cooldown_until else ""
        )

        if rem_sec > 0:
            rem_m = rem_sec // 60
            rem_s = rem_sec % 60
            poller_status = (
                f"🟡 <b>En pause anti-blocage (palier #{consecutive})</b>\n"
                f"<b>Motif :</b> {reason}\n"
                f"<b>Reprise dans :</b> {rem_m} min {rem_s:02d}s (à {resume_time})"
            )
        else:
            poller_status = "🟡 <b>Pause prévue terminée ; reprise non confirmée</b>"
    elif poller_data and poller_data.get("status") == "active":
        last_poll = poller_data.get("last_poll_at")
        if (
            isinstance(last_poll, bool) or not isinstance(last_poll, (int, float))
            or not 0 < last_poll <= now.timestamp()
        ):
            poller_status = "⚪ <b>Inconnu</b> (horodatage absent ou invalide)"
        else:
            elapsed = int(now.timestamp() - last_poll)
            if elapsed <= 120:
                poller_status = f"🟢 <b>Active</b> (scan -{elapsed}s)"
            else:
                elapsed_m = elapsed // 60
                elapsed_s = elapsed % 60
                poller_status = f"🟠 <b>En retard</b> (scan -{elapsed_m}m{elapsed_s:02d}s)"
    elif poller_data and poller_data.get("status") == "stopped":
        poller_status = "🔴 <b>Arrêté</b>"
    else:
        poller_status = "⚪ <b>Inconnu</b> (aucun état exploitable)"

    # Réception persistée → marquage, avant commit ; succès notifiés seulement.
    # Ni publication → découverte, ni délai d'un accusé ou d'une lecture.
    latency = await StatsRepository(db_session).get_notification_latency_ms(days=7)
    if latency["count"] and latency["p50_ms"] is not None:
        latency_line = f"<b>Réception → marquage :</b> {latency['p50_ms']} ms <i>(notifiés)</i>"
    else:
        latency_line = "<b>Réception → marquage :</b> Indisponible"

    cycle_p50 = (poller_data or {}).get("cycle_gap_p50_s")
    if cycle_p50:
        cadence_line = f"<b>Cadence de scrutation :</b> {cycle_p50:g} s"
    else:
        cadence_line = "<b>Cadence de scrutation :</b> Indisponible"

    scan_lines = [latency_line, cadence_line]
    scan_lines.extend(
        line
        for line in (
            _availability_line(poller_data),
            _searches_line(poller_data),
            _requests_line(poller_data),
        )
        if line
    )
    scan_detail = "\n".join(scan_lines)

    cat_counts = await StatsRepository(db_session).count_active_alerts_by_category()

    total_active = sum(row[3] for row in cat_counts)

    cat_lines = []
    for _slug, label, emoji, count in cat_counts:
        cat_lines.append(f"{emoji} <b>{label} :</b> {count}")

    cat_summary = "Aucune alerte active" if not cat_lines else "\n".join(cat_lines)

    night_hours_info = (
        f"{settings.NIGHT_MODE_START_HOUR:02d}:00 - {settings.NIGHT_MODE_END_HOUR:02d}:00"
        if settings.NIGHT_MODE_ENABLED
        else "Désactivée"
    )

    ai_info = await llm_client.check_health()
    if ai_info.status == "active":
        ai_status = "🟢 <b>Sonde accessible</b>"
    elif ai_info.status == "disabled":
        ai_status = "<b>Désactivé</b>"
    elif ai_info.status == "unconfigured":
        ai_status = "<b>Non configuré</b> (clé API manquante)"
    else:
        ai_status = f"🔴 <b>Indisponible</b> ({ai_info.message})"

    text = (
        "<b>État du service</b>\n\n"
        f"<b>Heure locale :</b> {time_str} ({tz_label})\n"
        f"<b>Scanner Vinted :</b> {poller_status}\n"
        f"<b>Scanner Leboncoin :</b> {leboncoin_status}\n"
        f"<b>Moteur IA</b> (sonde) : {ai_status}\n"
        f"<b>Plage de veille :</b> {night_hours_info}\n\n"
        "<b>Latence</b>\n"
        f"{scan_detail}\n\n"
        f"<b>Alertes actives ({total_active}) :</b>\n"
        f"{cat_summary}\n\n"
        "Utilise /list pour le détail ou /add pour en créer une."
    )

    await message.answer(text)
