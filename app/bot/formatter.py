import html

from app.core.constants import AlertStatus
from app.db.models.alert import Alert


def format_cents_to_euros(cents: int | None) -> str:
    """Formate un montant en centimes en euros (ex: 1500 -> '15,00 €')."""
    if cents is None:
        return "non défini"
    euros = cents / 100
    return f"{euros:.2f} €".replace(".", ",")


def format_alert_recap(
    emoji: str,
    name: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
    variants: list[str],
    max_price_cents: int | None,
    accepted_conditions: list[str] | None,
) -> str:
    """Génère le texte de récapitulatif lors de la création d'une alerte."""
    lines = [
        f"{emoji} Nouvelle alerte\n",
        f"Nom : {name}",
        f"Mots-clés : {', '.join(include_keywords)}",
    ]
    if exclude_keywords:
        lines.append(f"Exclusions : {', '.join(exclude_keywords)}")
    if variants:
        lines.append(f"Variantes : {', '.join(variants)}")

    if max_price_cents is not None:
        lines.append(f"Prix maximum article : {format_cents_to_euros(max_price_cents)}")
    else:
        lines.append("Prix maximum article : Aucun seuil")

    if accepted_conditions:
        lines.append(f"États : {', '.join(accepted_conditions)}")
    else:
        lines.append("États : Tous")

    return "\n".join(lines)


def format_alert_card(alert: Alert, current_index: int = 0, total_count: int = 1) -> str:
    """Génère la carte compacte d'une alerte pour /show."""
    status_emoji = "🟢 Active" if alert.status == AlertStatus.ACTIVE else "⏸ En pause"
    category_emoji = alert.category.emoji if alert.category else "📚"

    header = f"{category_emoji} <b>{html.escape(alert.name)}</b>"
    if total_count > 1:
        header += f" ({current_index + 1}/{total_count})"

    lines = [
        f"{header}\n",
    ]
    if getattr(alert, "isbn", None):
        lines.append(f"ISBN : {alert.isbn}")

    lines.extend([
        f"Prix max : {format_cents_to_euros(alert.max_item_price_cents)}",
        f"Inclus : {', '.join(alert.include_keywords)}",
    ])
    if alert.exclude_keywords:
        lines.append(f"Exclus : {', '.join(alert.exclude_keywords)}")
    if alert.variants:
        lines.append(f"Variantes : {', '.join(alert.variants)}")
    if alert.accepted_conditions:
        lines.append(f"États : {', '.join(alert.accepted_conditions)}")

    lines.append(f"Statut : {status_emoji}")

    if alert.vinted_search_url:
        lines.append(f'\nLien : <a href="{alert.vinted_search_url}">Consulter la recherche</a>')

    return "\n".join(lines)
