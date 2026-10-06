import asyncio
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

# Limite de légende de `send_photo` : au-delà, Telegram refuse la photo pour la
# légende, alors que le message texte en accepte 4096. L'information passe avant
# l'image — jamais l'inverse.
PHOTO_CAPTION_LIMIT = 1024

# Reprise après un refus de Telegram : le refus porte sur le contenu téléchargé à
# l'instant, une seconde tentative immédiate reprendrait le même résultat en cache.
PHOTO_RETRY_DELAY_SECONDS = 1.0


def build_listing_caption(
    listing: Listing,
    market_comparison_line: str | None = None,
    alert_keywords: list[str] | None = None,
) -> str:
    """Construit la légende de l'alerte d'annonce, dans l'ordre de la charte.

    Ordre : titre, prix et total estimé, état, bloc de comparaison si applicable,
    mots-clés déclencheurs si disponibles. Les informations absentes ne produisent
    pas de bloc vide.

    Chaque libellé de champ est gras, sa valeur reste en romain (charte,
    `app.bot.labels`) : la donnée se lit d'un coup d'œil sans que la ligne entière
    pèse sur l'écran.
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
        f"<b>État :</b> {safe_condition}",
    ]

    if market_comparison_line and market_comparison_line not in (
        "📉 Occasion : indisponible",
        "📊 Prix du marché : indisponible",
    ):
        lines.append("")
        lines.append(market_comparison_line)

    if alert_keywords:
        lines.append("")
        lines.append(f"<b>Mots-clés :</b> {join_list(alert_keywords)}")

    return "\n".join(lines)


def _photo_url_for_alert(listing: Listing, caption: str) -> str | None:
    """Couverture retenue pour l'alerte, ou `None` quand l'alerte part en texte.

    Une image absente, une URL impropre ou une légende trop longue ne font jamais
    échouer l'envoi : chacun de ces cas est nommé dans les journaux, parce qu'une
    alerte partie sans image ne doit plus être silencieuse.
    """
    if not listing.photos:
        logger.info("Alerte %s sans photo : aucune couverture, envoi en texte", listing.id)
        return None

    photo_url = listing.photos[0]
    if not isinstance(photo_url, str) or not photo_url.startswith("https://"):
        logger.warning(
            "Alerte %s : URL de photo invalide (%r), envoi en texte",
            listing.id,
            photo_url,
        )
        return None

    if len(caption) > PHOTO_CAPTION_LIMIT:
        logger.error(
            "Alerte %s : légende de %d caractères (limite photo %d), envoi en texte",
            listing.id,
            len(caption),
            PHOTO_CAPTION_LIMIT,
        )
        return None

    return photo_url


async def send_listing_alert(
    bot: Bot,
    chat_id: int,
    listing: Listing,
    market_comparison_line: str | None = None,
    alert_keywords: list[str] | None = None,
    alert: Any | None = None,
) -> bool:
    """Envoie l'alerte à l'utilisateur Telegram avec photo principale et clavier adapté.

    La photo est tentée deux fois par URL avant tout repli : Telegram télécharge lui-
    même le contenu, et un CDN qui renvoie une page HTML à sa place fait échouer la
    première tentative sans que l'annonce soit en faute. Le repli texte reste garanti —
    une panne photo ne fait jamais perdre l'alerte.
    """
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
        photo_url = _photo_url_for_alert(listing, caption)
        if photo_url is not None:
            for attempt in (1, 2):
                try:
                    sent_msg = await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_url,
                        caption=caption,
                        reply_markup=keyboard,
                    )
                    break
                except Exception as photo_exc:
                    logger.error(
                        "Échec %d/2 de l'envoi de la photo (listing %s, %s): %s",
                        attempt,
                        listing.id,
                        photo_url,
                        photo_exc,
                    )
                    if attempt == 1:
                        await asyncio.sleep(PHOTO_RETRY_DELAY_SECONDS)
            if sent_msg is None:
                logger.error(
                    "Photo non envoyée après 2 tentatives (listing %s), repli sur message texte",
                    listing.id,
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

