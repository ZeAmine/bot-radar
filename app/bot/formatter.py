import html

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
    format_conditions,
)
from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.utils.price import format_cents


def format_cents_to_euros(cents: int | None) -> str:
    """Formate un montant en centimes en euros (ex: 1500 -> '15,00 €').

    Dépend du helper canonique : le cas ``None`` — seuil absent — est rendu
    « Non renseigné », jamais zéro.
    """
    if cents is None:
        return NOT_PROVIDED
    return format_cents(cents)


def format_alert_card(alert: Alert, current_index: int = 0, total_count: int = 1) -> str:
    """Génère la fiche d'une alerte pour /show, dans l'ordre de la charte.

    Ordre : nom, statut, Prix maximum, identification (ISBN), États acceptés,
    bloc de filtres, position dans la navigation. Les libellés et le vocabulaire
    viennent de `app.bot.labels` : un statut est en toutes lettres, les états en
    libellés complets, et une valeur dynamique est toujours échappée.

    Chaque libellé de champ est gras, sa valeur reste en romain (charte, règle 1) :
    la fiche se balaie du regard, sans qu'une ligne entière pèse sur l'écran.
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
                    f"<b>{label} :</b> {html.escape(str(value)) if value else NOT_PROVIDED}"
                )

    lines.extend(
        [
            f"<b>{FIELD_STATUS} :</b> {status_text}",
            f"<b>{FIELD_MAX_PRICE} :</b> {format_cents_to_euros(alert.max_item_price_cents)}",
            f"<b>{FIELD_CONDITIONS} :</b> {format_conditions(alert.accepted_conditions)}",
            f"<b>{FIELD_INCLUDE_KEYWORDS} :</b> "
            f"{', '.join(html.escape(kw) for kw in alert.include_keywords)}",
        ]
    )
    if alert.exclude_keywords:
        excluded = ", ".join(html.escape(kw) for kw in alert.exclude_keywords)
        lines.append(f"<b>{FIELD_EXCLUDE_KEYWORDS} :</b> {excluded}")
    if alert.variants:
        lines.append(
            f"<b>{FIELD_VARIANTS} :</b> {', '.join(html.escape(v) for v in alert.variants)}"
        )

    if alert.vinted_search_url:
        lines.append(
            f'\n<b>Lien :</b> <a href="{alert.vinted_search_url}">Consulter la recherche</a>'
        )

    return "\n".join(lines)
