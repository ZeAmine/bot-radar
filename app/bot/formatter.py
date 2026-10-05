import html

from app.bot.labels import (
    NOT_PROVIDED,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    format_conditions_full,
)
from app.core.constants import AlertStatus, ItemCondition
from app.db.models.alert import Alert
from app.utils.price import format_cents

# Abréviations d'états pour les cartes compactes. Présentation pure : c'est pourquoi
# elles vivent ici et non dans app/core, où seuls les libellés d'ItemCondition font foi.
_CONDITION_SHORT = {
    ItemCondition.NEW_WITH_TAG.value: "NAE",
    ItemCondition.NEW_WITHOUT_TAG.value: "NSE",
    ItemCondition.VERY_GOOD.value: "TBE",
    ItemCondition.GOOD.value: "BE",
    ItemCondition.SATISFACTORY.value: "SAT",
}


def format_conditions_short(conditions: list[str] | None) -> str:
    """Abrège les états acceptés (« NAE, NSE, TBE ») pour tenir sur une seule ligne.

    Un libellé hors référentiel est rendu tel quel : une abréviation manquante ne doit
    jamais effacer un critère d'acceptation de l'alerte.
    """
    return ", ".join(
        _CONDITION_SHORT.get(condition, condition) for condition in (conditions or [])
    )


def format_cents_to_euros(cents: int | None) -> str:
    """Formate un montant en centimes en euros (ex: 1500 -> '15,00 €').

    Dépend du helper canonique : le cas ``None`` — seuil absent — est rendu
    « Non renseigné », jamais zéro.
    """
    if cents is None:
        return NOT_PROVIDED
    return format_cents(cents)


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
    """Génère la fiche d'une alerte pour /show, dans l'ordre de la charte.

    Ordre : nom, statut, Prix maximum, identification (ISBN), États acceptés,
    bloc de filtres, position dans la navigation. Les libellés et le vocabulaire
    viennent de `app.bot.labels` : un statut est en toutes lettres, les états en
    libellés complets, et une valeur dynamique est toujours échappée.
    """
    status_text = STATUS_ACTIVE if alert.status == AlertStatus.ACTIVE else STATUS_PAUSED
    category_emoji = alert.category.emoji if alert.category else "📚"

    header = f"{category_emoji} <b>{html.escape(alert.name)}</b>"
    if total_count > 1:
        header += f" ({current_index + 1}/{total_count})"

    lines = [
        f"{header}\n",
    ]
    isbn = getattr(alert, "isbn", None)
    if isbn:
        lines.append(f"ISBN : <code>{html.escape(isbn)}</code>")

    lines.extend([
        f"Statut : {status_text}",
        f"Prix maximum : {format_cents_to_euros(alert.max_item_price_cents)}",
        f"États acceptés : {format_conditions_full(alert.accepted_conditions)}",
        f"Mots-clés inclus : {', '.join(html.escape(kw) for kw in alert.include_keywords)}",
    ])
    if alert.exclude_keywords:
        excluded = ", ".join(html.escape(kw) for kw in alert.exclude_keywords)
        lines.append(f"Mots-clés exclus : {excluded}")
    if alert.variants:
        lines.append(f"Variantes : {', '.join(html.escape(v) for v in alert.variants)}")

    if alert.vinted_search_url:
        lines.append(f'\nLien : <a href="{alert.vinted_search_url}">Consulter la recherche</a>')

    return "\n".join(lines)
