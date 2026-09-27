"""Service métier pour la collecte et le formatage des statistiques d'activité."""

import html
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.stats_repo import StatsRepository

CATEGORY_NAMES: dict[str, str] = {
    "bd-comics": "📚 Livres",
    "bd_comics": "📚 Livres",
    "vetements": "👕 Vêtements",
    "clothes": "👕 Vêtements",
    "tech": "💻 Électronique",
    "gaming": "🎮 Jeux Vidéo",
}


class StatisticsService:
    """Service de génération des rapports statistiques de BotRadar."""

    def __init__(self, session: AsyncSession) -> None:
        self.repo = StatsRepository(session)

    async def get_stats_data(self, days: int = 7) -> dict[str, Any]:
        """Récupère les données statistiques brutes pour une période donnée."""
        return await self.repo.get_period_stats(days=days)

    async def get_formatted_stats(self, days: int = 7) -> str:
        """Génère le message HTML formaté des statistiques pour Telegram."""
        data = await self.get_stats_data(days=days)
        return self.format_stats_message(data)

    @staticmethod
    def format_stats_message(data: dict[str, Any]) -> str:
        """Formate le dictionnaire de statistiques en message Telegram HTML."""
        days = data.get("days", 7)
        active_alerts = data.get("active_alerts", 0)
        category_alerts = data.get("category_alerts", [])
        received_listings = data.get("received_listings", 0)
        matched_listings = data.get("matched_listings", 0)
        sent_notifications = data.get("sent_notifications", 0)
        raw_top_category = data.get("top_category", "Aucune")
        top_category_display = CATEGORY_NAMES.get(
            raw_top_category, html.escape(str(raw_top_category))
        )

        cat_breakdown = ""
        if category_alerts:
            cat_parts = [
                f"{ca.get('emoji', '')} {ca.get('count', 0)}"
                for ca in category_alerts
                if ca.get("count", 0) > 0
            ]
            if cat_parts:
                cat_breakdown = f" ({' • '.join(cat_parts)})"

        match_rate_str = ""
        if received_listings > 0:
            pct = (matched_listings / received_listings) * 100
            match_rate_str = f" <i>({pct:.1f}%)</i>"

        top_alerts = data.get("top_alerts", [])
        bd_available = data.get("bd_comparisons_available", 0)
        bd_unavailable = data.get("bd_comparisons_unavailable", 0)

        top_alerts_lines: list[str] = []
        if top_alerts:
            for item in top_alerts:
                if len(item) >= 3 and item[2]:
                    prefix = f"{item[2]} "
                    name = item[0]
                    count = item[1]
                else:
                    prefix = ""
                    name = item[0]
                    count = item[1]
                top_alerts_lines.append(f"• {prefix}<b>{html.escape(str(name))}</b> : {count} alerte(s)")
        else:
            top_alerts_lines.append("• <i>Aucune alerte déclenchée sur la période</i>")

        top_alerts_text = "\n".join(top_alerts_lines)

        return (
            f"📊 <b>Statistiques BotRadar ({days} derniers jours)</b>\n\n"
            f"🔔 <b>Alertes actives :</b> {active_alerts}{cat_breakdown}\n"
            f"📥 <b>Annonces reçues :</b> {received_listings}\n"
            f"🎯 <b>Annonces matchées :</b> {matched_listings}{match_rate_str}\n"
            f"💬 <b>Alertes Telegram envoyées :</b> {sent_notifications}\n\n"
            f"🏷️ <b>Catégorie la plus active :</b> {top_category_display}\n\n"
            f"🏆 <b>Top alertes déclenchées :</b>\n"
            f"{top_alerts_text}\n\n"
            f"📚 <b>Comparaisons de prix Livres :</b>\n"
            f"• Disponibles : {bd_available}\n"
            f"• Indisponibles / sans offre : {bd_unavailable}"
        )
