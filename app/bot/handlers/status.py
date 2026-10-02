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
from app.tasks.vinted_poller import get_poller_status, is_night_time

router = Router(name="status_router")


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

    if night_active:
        poller_status = "<b>Mode nuit</b>"
    elif not settings.VINTED_POLLER_ENABLED:
        poller_status = "⏸️ <b>Désactivé</b>"
    elif poller_data and poller_data.get("status") == "cooldown":
        cooldown_until = poller_data.get("cooldown_until") or 0
        rem_sec = max(0, int(cooldown_until - now.timestamp()))
        consecutive = poller_data.get("consecutive_blocks", 1)
        reason = poller_data.get("reason", "Blocage Cloudflare (403)")
        resume_time = poller_data.get("resume_time") or (
            datetime.fromtimestamp(cooldown_until, tz).strftime("%H:%M") if cooldown_until else ""
        )

        if rem_sec > 0:
            rem_m = rem_sec // 60
            rem_s = rem_sec % 60
            poller_status = (
                f"🟡 <b>En pause anti-blocage (palier #{consecutive})</b>\n"
                f"   └  ⚠️ Motif : {reason}\n"
                f"   └  ⏳ Reprise dans : <b>{rem_m} min {rem_s:02d}s</b> (à {resume_time})"
            )
        else:
            poller_status = "🟡 <b>Fin de pause : reprise imminente du scan...</b>"
    elif poller_data and poller_data.get("status") == "active":
        last_poll = poller_data.get("last_poll_at", 0)
        elapsed = max(0, int(now.timestamp() - last_poll))
        if elapsed <= 120:
            poller_status = f"🟢 <b>Active</b> (scan -{elapsed}s)"
        else:
            elapsed_m = elapsed // 60
            elapsed_s = elapsed % 60
            poller_status = (
                f"🟠 <b>En retard</b> (scan -{elapsed_m}m{elapsed_s:02d}s)"
            )
    else:
        poller_status = "🟢 <b>Active</b>"

    cat_counts = await StatsRepository(db_session).count_active_alerts_by_category()

    total_active = sum(row[3] for row in cat_counts)

    cat_lines = []
    for _slug, label, emoji, count in cat_counts:
        cat_lines.append(f"   └  {emoji} {label} : <b>{count}</b>")

    cat_summary = "   └  <i>Aucune alerte active</i>" if not cat_lines else "\n".join(cat_lines)

    night_hours_info = (
        f"{settings.NIGHT_MODE_START_HOUR:02d}:00 - {settings.NIGHT_MODE_END_HOUR:02d}:00"
        if settings.NIGHT_MODE_ENABLED
        else "Désactivée"
    )

    ai_info = await llm_client.check_health()
    if ai_info.status == "active":
        ai_status = "🟢 <b>Actif</b>"
    elif ai_info.status == "disabled":
        ai_status = "⏸️ <b>Désactivé</b>"
    elif ai_info.status == "unconfigured":
        ai_status = "⚪ <b>Non configuré</b> (clé API manquante)"
    else:
        ai_status = f"🔴 <b>Indisponible</b> ({ai_info.message})"

    text = (
        "📡  <b>État du Service</b>\n\n"
        f"🕒 <b>Heure locale</b> : {time_str} ({tz_label})\n"
        f"🔍 <b>Scanner Vinted</b> : {poller_status}\n"
        f"🤖 <b>Moteur IA ({ai_info.model})</b> : {ai_status}\n"
        f"🌙 <b>Plage de veille</b> : {night_hours_info}\n\n"
        f"🎯 <b>Alertes actives ({total_active}) :</b>\n"
        f"{cat_summary}\n\n"
        "💡 <i>Utilisez /list pour voir le détail ou /add pour en créer une.</i>"
    )

    await message.answer(text)
