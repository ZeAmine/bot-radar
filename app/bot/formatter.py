import html
from typing import Any

from app.bot.labels import (
    FIELD_AUTHOR,
    FIELD_CONDITIONS,
    FIELD_EDITION,
    FIELD_EXCLUDE_KEYWORDS,
    FIELD_INCLUDE_KEYWORDS,
    FIELD_ISBN,
    FIELD_MAX_PRICE,
    FIELD_PUBLISHER,
    FIELD_STATUS,
    FIELD_VARIANTS,
    NOT_PROVIDED,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    escape_display,
    format_conditions,
)
from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.domain.entities.alert_preparation import preparation_request
from app.utils.price import format_cents


def format_cents_to_euros(cents: int | None) -> str:
    """Formate un montant en centimes en euros (ex: 1500 -> '15,00 €').

    Dépend du helper canonique : le cas ``None`` — seuil absent — est rendu
    « Non renseigné », jamais zéro.
    """
    if cents is None:
        return NOT_PROVIDED
    return format_cents(cents)


def alert_status_text(alert: Alert) -> str:
    """Sépare surveillance active, pause volontaire et travail de préparation."""
    request = preparation_request(alert)
    if alert.status == AlertStatus.ACTIVE:
        return STATUS_ACTIVE
    if request is None:
        return STATUS_PAUSED
    if not request.activation_requested:
        return "⏸ Pause volontaire - préparation inachevée"
    if request.state == "blocked":
        return "⚠️ Préparation bloquée - relance disponible"
    return "⏳ Préparation en cours ou en attente de reprise"


def format_alert_card(alert: Alert, current_index: int = 0, total_count: int = 1) -> str:
    """Génère la fiche d'une alerte pour /show, dans l'ordre de la charte.

    Ordre : nom, statut, Prix maximum, identification (ISBN), États acceptés,
    bloc de filtres, position dans la navigation. Les libellés et le vocabulaire
    viennent de `app.bot.labels` : un statut est en toutes lettres, les états en
    libellés complets, et une valeur dynamique est toujours échappée.

    Chaque libellé de champ est gras, sa valeur reste en romain (charte, règle 1) :
    la fiche se balaie du regard, sans qu'une ligne entière pèse sur l'écran.
    """
    status_text = alert_status_text(alert)
    category_emoji = alert.category.emoji if alert.category else "📚"

    header = f"{category_emoji} <b>{escape_display(alert.name)}</b>"
    if total_count > 1:
        header += f" ({current_index + 1}/{total_count})"

    lines = [
        f"{header}\n",
    ]
    isbn = getattr(alert, "isbn", None)
    if isbn:
        lines.append(f"<b>{FIELD_ISBN} :</b> <code>{html.escape(isbn)}</code>")
        links = alert.__dict__.get("alert_books", [])
        book = (
            next((link.book for link in links if link.book.isbn13 == isbn), None)
            if isinstance(links, list)
            else None
        )
        if book is not None:
            for label, value in (
                (FIELD_AUTHOR, book.author),
                (FIELD_PUBLISHER, book.publisher),
                ("Année", book.publication_year),
                (FIELD_EDITION, book.edition),
            ):
                lines.append(
                    f"<b>{label} :</b> "
                    f"{escape_display(str(value)) if value else NOT_PROVIDED}"
                )

    lines.extend(
        [
            f"<b>{FIELD_STATUS} :</b> {status_text}",
            f"<b>{FIELD_MAX_PRICE} :</b> {format_cents_to_euros(alert.max_item_price_cents)}",
            f"<b>{FIELD_CONDITIONS} :</b> {format_conditions(alert.accepted_conditions)}",
            f"<b>{FIELD_INCLUDE_KEYWORDS} :</b> "
            f"{', '.join(escape_display(kw) for kw in alert.include_keywords)}",
        ]
    )
    if alert.exclude_keywords:
        excluded = ", ".join(escape_display(kw) for kw in alert.exclude_keywords)
        lines.append(f"<b>{FIELD_EXCLUDE_KEYWORDS} :</b> {excluded}")
    if alert.variants:
        lines.append(
            f"<b>{FIELD_VARIANTS} :</b> {', '.join(escape_display(v) for v in alert.variants)}"
        )

    if alert.vinted_search_url:
        lines.append(
            f'\n<b>Lien :</b> <a href="{alert.vinted_search_url}">Consulter la recherche</a>'
        )

    return "\n".join(lines)


async def send_alert_card(
    target: Any,
    alert: Alert,
    *,
    current_index: int = 0,
    total_count: int = 1,
    reply_markup: Any = None,
    edit: bool = False,
    suffix: str | None = None,
    **kwargs: Any,
) -> Any:
    """Envoie la fiche d'une alerte sans aperçu de lien, et rend le message produit.

    La carte porte un lien vers la recherche Vinted. Telegram en déploie un aperçu
    qui occupe plusieurs lignes et repousse les boutons hors de l'écran ; le lien,
    lui, doit rester cliquable. L'option qui l'empêche vit donc ici, à côté du
    gabarit qui produit le lien : un écran qui rend la carte par ce helper ne peut
    pas l'oublier, et `edit_text` la reprend comme `answer` — Telegram ne retire pas
    un aperçu déjà affiché, il faut rejouer l'appel avec l'option.

    `edit=True` remplace le message existant (navigation, retour d'édition) au lieu
    d'en envoyer un nouveau. `suffix` ajoute un bloc après la carte — la cote qui
    vient d'être relue, par exemple — sans que l'appelant ait à recomposer le texte.
    """
    text = format_alert_card(alert, current_index=current_index, total_count=total_count)
    if suffix:
        text += suffix
    if edit:
        return await target.edit_text(
            text, reply_markup=reply_markup, disable_web_page_preview=True, **kwargs
        )
    return await target.answer(
        text, reply_markup=reply_markup, disable_web_page_preview=True, **kwargs
    )
