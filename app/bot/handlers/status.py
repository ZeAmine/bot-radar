"""Handler pour la commande /status."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

from app.core.config import settings
from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.tasks.vinted_poller import (
    get_night_mode_remaining_seconds,
    get_poller_status,
    is_night_time,
)

router = Router(name="status_router")


@router.message(Command("status"))
async def handle_status(message: Message, db_session: Any) -> None:
    """Affiche l'état opérationnel en direct de Radar Bot."""
    try:
        tz = ZoneInfo(settings.NIGHT_MODE_TIMEZONE)
    except Exception:
        tz = ZoneInfo("Europe/Paris")
    now = datetime.now(tz)
    time_str = now.strftime("%H:%M")

    night_active = is_night_time(now)
    poller_data = await get_poller_status()

    if night_active:
        remaining = get_night_mode_remaining_seconds(now)
        hours_left = int(remaining // 3600)
        mins_left = int((remaining % 3600) // 60)
        poller_status = (
            f"🌙 <b>Mode Nuit actif</b> (veille jusqu'à {settings.NIGHT_MODE_END_HOUR:02d}:00, "
            f"encore {hours_left}h{mins_left:02d})"
        )
    elif not settings.VINTED_POLLER_ENABLED:
        poller_status = "⏸️ <b>Surveillance Vinted désactivée</b>"
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
                f"  ⚠️ Motif : {reason}\n"
                f"  ⏳ Reprise dans : <b>{rem_m} min {rem_s:02d}s</b> (à {resume_time})"
            )
        else:
            poller_status = "🟡 <b>Fin de pause : reprise imminente du scan...</b>"
    elif poller_data and poller_data.get("status") == "active":
        last_poll = poller_data.get("last_poll_at", 0)
        elapsed = max(0, int(now.timestamp() - last_poll))
        if elapsed <= 120:
            poller_status = f"🟢 <b>Surveillance Vinted active</b> (scan il y a {elapsed}s)"
        else:
            elapsed_m = elapsed // 60
            elapsed_s = elapsed % 60
            poller_status = (
                f"🟠 <b>Surveillance en retard</b> (dernier scan il y a {elapsed_m}m{elapsed_s:02d}s)"
            )
    else:
        poller_status = "🟢 <b>Surveillance Vinted active</b>"

    # Répartition des alertes actives par catégorie
    stmt = (
        select(Category.slug, Category.label, Category.emoji, func.count(Alert.id))
        .join(Alert, Alert.category_id == Category.id)
        .where(Alert.status == AlertStatus.ACTIVE, Alert.deleted_at.is_(None))
        .group_by(Category.slug, Category.label, Category.emoji)
        .order_by(func.count(Alert.id).desc())
    )
    result = await db_session.execute(stmt)
    cat_counts = result.all()

    total_active = sum(row[3] for row in cat_counts)

    cat_lines = []
    for _slug, label, emoji, count in cat_counts:
        cat_lines.append(f"  • {emoji} {label} : <b>{count}</b>")

    if not cat_lines:
        cat_summary = "  <i>Aucune alerte active</i>"
    else:
        cat_summary = "\n".join(cat_lines)

    night_hours_info = (
        f"{settings.NIGHT_MODE_START_HOUR:02d}:00 à {settings.NIGHT_MODE_END_HOUR:02d}:00"
        if settings.NIGHT_MODE_ENABLED
        else "Désactivé"
    )

    text = (
        "📡 <b>État du Radar Bot</b>\n\n"
        f"⏱ <b>Heure locale (Paris)</b> : {time_str}\n"
        f"🛡️ <b>Surveillance</b> : {poller_status}\n"
        f"🌙 <b>Heures creuses (nuit)</b> : {night_hours_info}\n\n"
        f"📊 <b>Alertes actives ({total_active}) :</b>\n"
        f"{cat_summary}\n\n"
        "💡 <i>Utilisez /list pour voir le détail ou /add pour en créer une.</i>"
    )

    await message.answer(text)
