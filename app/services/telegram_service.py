import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.bot.keyboards.listings import get_listing_alert_keyboard
from app.core.config import settings
from app.core.constants import CategorySlug
from app.core.tasks import safe_create_task
from app.db.models.listing import Listing
from app.services.matcher_service import CONDITION_ALIASES
from app.services.price_calculator import format_price_summary
from app.services.vision_guard_service import VisionGuardService

logger = logging.getLogger(__name__)


def build_listing_caption(
    listing: Listing,
    market_comparison_line: str | None = None,
    alert_keywords: list[str] | None = None,
    is_bundle: bool = False,
) -> str:
    """Construit la légende textuelle de l'alerte d'annonce selon le format demandé."""
    raw_condition = listing.condition or ""
    condition_text = (
        CONDITION_ALIASES.get(raw_condition.lower(), raw_condition)
        if raw_condition
        else "Non précisé"
    )
    price_line = format_price_summary(
        item_price_cents=listing.item_price_cents,
        estimated_total_cents=listing.estimated_total_cents,
    )

    safe_title = html.escape(listing.title)
    safe_condition = html.escape(condition_text)

    lines: list[str] = []
    if is_bundle:
        lines.append("📦 <b>DÉTECTION LOT AVANTAGEUX</b>")
        lines.append("<i>Cet ensemble contient votre recherche parmi d'autres articles !</i>\n")

    lines.extend([
        f"🆕 <b>{safe_title}</b>",
        f"🏷️ {safe_condition}",
        price_line,
    ])

    if market_comparison_line:
        lines.append("")
        lines.append(market_comparison_line)
    elif listing.category_slug == CategorySlug.BD_COMICS.value:
        lines.append("")
        lines.append("📉 Occasion : indisponible")

    if alert_keywords:
        keywords_str = ", ".join(html.escape(kw) for kw in alert_keywords)
        lines.append("")
        lines.append(f"🔍 Mots-clés : {keywords_str}")

    return "\n".join(lines)


async def send_listing_alert(
    bot: Bot,
    chat_id: int,
    listing: Listing,
    market_comparison_line: str | None = None,
    alert_keywords: list[str] | None = None,
    is_bundle: bool = False,
) -> bool:
    """Envoie l'alerte à l'utilisateur Telegram avec photo principale et clavier adapté."""
    caption = build_listing_caption(
        listing,
        market_comparison_line=market_comparison_line,
        alert_keywords=alert_keywords,
        is_bundle=is_bundle,
    )
    checkout_url = f"{listing.url}#radar_autobuy=1"
    keyboard = get_listing_alert_keyboard(
        listing_id=listing.id,
        listing_url=listing.url,
        category_slug=listing.category_slug,
        checkout_url=checkout_url,
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
            except TelegramAPIError as photo_exc:
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

        # Vision Guard : Analyse asynchrone non-bloquante en arrière-plan (Tech, Gaming & Livres)
        if sent_msg and listing.category_slug in (
            CategorySlug.TECH.value,
            CategorySlug.BD_COMICS.value,
            CategorySlug.GAMING.value,
        ):
            safe_create_task(
                VisionGuardService.check_and_notify(
                    bot=bot,
                    chat_id=chat_id,
                    message_id=sent_msg.message_id,
                    listing=listing,
                ),
                name=f"vision_guard_{listing.id}",
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
    """Diffuse un message d'état système à tous les utilisateurs Telegram autorisés."""
    allowed_ids = settings.TELEGRAM_ALLOWED_USER_IDS
    if not allowed_ids:
        return

    should_close_bot = False
    if bot is None:
        token = settings.TELEGRAM_BOT_TOKEN.get_secret_value()
        if not token:
            return
        bot = Bot(token=token)
        should_close_bot = True

    try:
        for user_id in allowed_ids:
            try:
                await bot.send_message(
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
    finally:
        if should_close_bot:
            await bot.session.close()

