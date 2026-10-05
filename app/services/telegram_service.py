import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.bot.keyboards.listings import get_listing_alert_keyboard
from app.bot.labels import abbreviate_condition, escape_html, join_list
from app.core.config import settings
from app.core.tasks import safe_create_task
from app.db.models.listing import Listing
from app.services.ai.pipeline import ai_post_alert_pipeline
from app.services.matcher_service import CONDITION_ALIASES
from app.services.price_calculator import format_price_summary

logger = logging.getLogger(__name__)


def build_listing_caption(
    listing: Listing,
    market_comparison_line: str | None = None,
    alert_keywords: list[str] | None = None,
) -> str:
    """Construit la légende de l'alerte d'annonce, dans l'ordre de la charte.

    Ordre : titre, prix et total estimé, état, bloc de comparaison si applicable,
    mots-clés déclencheurs si disponibles. Les informations absentes ne produisent
    pas de bloc vide.
    """
    raw_condition = listing.condition or ""
    condition_text = CONDITION_ALIASES.get(raw_condition.lower(), raw_condition)
    price_line = format_price_summary(
        item_price_cents=listing.item_price_cents,
        estimated_total_cents=listing.estimated_total_cents,
    )

    safe_title = escape_html(listing.title)
    safe_condition = escape_html(abbreviate_condition(condition_text))

    lines: list[str] = [
        f"🆕 <b>{safe_title}</b>",
        price_line,
        f"État : {safe_condition}",
    ]

    if market_comparison_line and market_comparison_line not in (
        "📉 Occasion : indisponible",
        "📊 Prix du marché : indisponible",
    ):
        lines.append("")
        lines.append(market_comparison_line)

    if alert_keywords:
        lines.append("")
        lines.append(f"Mots-clés : {join_list(alert_keywords)}")

    return "\n".join(lines)


async def send_listing_alert(
    bot: Bot,
    chat_id: int,
    listing: Listing,
    market_comparison_line: str | None = None,
    alert_keywords: list[str] | None = None,
    alert: Any | None = None,
) -> bool:
    """Envoie l'alerte à l'utilisateur Telegram avec photo principale et clavier adapté."""
    caption = build_listing_caption(
        listing,
        market_comparison_line=market_comparison_line,
        alert_keywords=alert_keywords,
    )
    keyboard = get_listing_alert_keyboard(
        listing_id=listing.id,
        listing_url=listing.url,
        category_slug=listing.category_slug,
    )

    sent_msg = None
    try:
        if listing.photos and len(listing.photos) > 0:
            try:
                sent_msg = await bot.send_photo(
                    chat_id=chat_id,
                    photo=listing.photos[0],
                    caption=caption,
                    reply_markup=keyboard,
                )
            except Exception as photo_exc:
                logger.warning(
                    "Échec d'envoi de la photo (%s), repli sur message texte: %s",
                    listing.photos[0],
                    photo_exc,
                )

        if not sent_msg:
            sent_msg = await bot.send_message(
                chat_id=chat_id,
                text=caption,
                reply_markup=keyboard,
            )

        # Pipeline IA asynchrone, post-alerte et non bloquant
        if sent_msg and settings.AI_ENABLED:
            safe_create_task(
                ai_post_alert_pipeline.enrich_alert(
                    bot=bot,
                    chat_id=chat_id,
                    message_id=sent_msg.message_id,
                    listing=listing,
                    alert=alert,
                ),
                name=f"ai_enrich_{listing.id}",
            )

        return True
    except TelegramAPIError as exc:
        logger.error(
            "Échec de l'envoi de l'alerte Telegram (chat_id: %s, listing_id: %s): %s",
            chat_id,
            listing.id,
            exc,
        )
        return False


async def notify_admins(
    text: str,
    bot: Bot | None = None,
) -> None:
    """Diffuse un message d'état système à tous les utilisateurs Telegram autorisés.

    Le client par défaut est l'instance partagée du bot : en créer un à chaque
    notification ouvrait une nouvelle session réseau pour un envoi, alors que le
    cycle de vie du client est déjà géré par le processus.
    """
    allowed_ids = settings.TELEGRAM_ALLOWED_USER_IDS
    if not allowed_ids:
        return

    target_bot = bot
    if target_bot is None:
        from app.bot.bot_instance import bot as default_bot

        target_bot = default_bot

    for user_id in allowed_ids:
        try:
            await target_bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning(
                "Impossible d'envoyer la notification système à l'utilisateur %s: %s",
                user_id,
                exc,
            )

