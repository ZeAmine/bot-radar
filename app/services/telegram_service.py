import asyncio
import logging
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Literal, TypeGuard

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardMarkup, Message

from app.bot.keyboards.listings import listing_alert_keyboard
from app.bot.labels import abbreviate_condition, escape_html, join_list
from app.core import alert_latency as latency
from app.core.config import settings
from app.core.tasks import safe_create_task
from app.db.models.listing import Listing
from app.services.ai.pipeline import ai_post_alert_pipeline
from app.services.matcher_service import CONDITION_ALIASES
from app.services.price_calculator import format_price_summary

logger = logging.getLogger(__name__)

# Limite de légende de `send_photo` : au-delà, Telegram refuse la photo, alors
# que le message texte en accepte 4096. L'information passe avant l'image.
PHOTO_CAPTION_LIMIT = 1024

# Reprise après refus : le refus porte sur le contenu téléchargé, une tentative
# immédiate reprendrait le même résultat en cache.
PHOTO_RETRY_DELAY_SECONDS = 1.0


def usable_receipt(value: object, chat_id: int | None = None) -> TypeGuard[Message]:
    """Reçu de l'API ; zéro ou mauvais destinataire reste inconnu, sans preuve de lecture."""
    return (
        isinstance(value, Message)
        and type(value.message_id) is int
        and value.message_id > 0
        and (chat_id is None or value.chat.id == chat_id)
    )


@dataclass(frozen=True)
class DeliveryResult:
    """Observation du sender ; l'orchestrateur persiste les champs utiles du reçu."""
    outcome: Literal["not_sent", "acknowledged", "rejected", "unknown"]
    transport: Literal["photo", "text", "none"]
    receipt: Message | None = None
    reason: str = ""
    error_type: str | None = None
    retry_after: int | None = None

    def __bool__(self) -> bool:
        return self.outcome == "acknowledged" and usable_receipt(self.receipt)

    def diagnostics(
        self,
        *,
        alert_id: uuid.UUID | None,
        listing_id: uuid.UUID,
        attempt_id: uuid.UUID,
        persistence_stage: str,
        **details: Any,
    ) -> dict[str, Any]:
        return {
            "event": "notification_delivery",
            "alert_id": str(alert_id) if alert_id else None,
            "listing_id": str(listing_id),
            "attempt_id": str(attempt_id),
            "transport": self.transport,
            "delivery_result": self.outcome,
            "message_id": self.receipt.message_id if usable_receipt(self.receipt) else None,
            "reason": self.reason,
            "error_type": self.error_type,
            "persistence_stage": persistence_stage,
            **details,
        }


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
        latency.safe_log(
            logger,
            logging.INFO,
            f"Alerte {listing.id} sans photo : aucune couverture, envoi en texte",
            {},
        )
        return None

    photo_url = listing.photos[0]
    if not isinstance(photo_url, str) or not photo_url.startswith("https://"):
        latency.safe_log(
            logger,
            logging.WARNING,
            f"Alerte {listing.id} : URL de photo invalide, envoi en texte",
            {},
        )
        return None

    if len(caption) > PHOTO_CAPTION_LIMIT:
        latency.safe_log(
            logger,
            logging.ERROR,
            f"Alerte {listing.id} : légende de {len(caption)} caractères "
            f"(limite photo {PHOTO_CAPTION_LIMIT}), envoi en texte",
            {},
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
    attempt_id: uuid.UUID | None = None,
) -> DeliveryResult:
    """Envoie l'alerte à l'utilisateur Telegram avec photo principale et clavier adapté.

    Un refus photo reconnu permet une seconde tentative puis un repli texte.
    Un résultat inconnu arrête tout nouvel appel. L'accusé de l'API ne prouve
    jamais une lecture par l'utilisateur.
    """
    attempt_id = attempt_id or uuid.uuid4()
    alert_id = getattr(alert, "id", None)

    timing = latency.current() or latency.AlertTiming()
    timing.listing_id, timing.alert_id, timing.attempt_id = listing.id, alert_id, attempt_id
    started_at = latency.clock()
    completed = False
    terminal_result = "unknown"
    terminal_error = None
    result: DeliveryResult | None = None
    with latency.timing_context(timing):
        try:

            def diagnostic(result: DeliveryResult, number: int = 0) -> None:
                latency.safe_log(
                    logger,
                    logging.INFO if result else logging.ERROR,
                    "Résultat de la tentative Telegram.",
                    result.diagnostics(
                        alert_id=alert_id,
                        listing_id=listing.id,
                        attempt_id=attempt_id,
                        persistence_stage="not_started",
                        transport_attempt=number,
                    ),
                )

            # Ces opérations sont locales : leur échec précède tout appel client.
            try:
                caption = build_listing_caption(listing, market_comparison_line, alert_keywords)
                keyboard = listing_alert_keyboard(listing)
                photo_url = _photo_url_for_alert(listing, caption)
            except Exception as exc:
                result = DeliveryResult(
                    "not_sent", "none", reason="local_validation", error_type=type(exc).__name__
                )
                diagnostic(result)
                completed = True
                return result


            async def request(
                transport: Literal["photo", "text"], operation: Awaitable[Message], number: int
            ) -> tuple[DeliveryResult, bool]:
                """Le booléen indique une exception ; seul un refus reconnu autorise un repli."""
                started_at = latency.clock()
                first_call = not timing.first_call_seen
                if first_call:
                    timing.first_call_seen = True
                    timing.first_call_at = started_at

                def record_transport(result: str, ended_at: float | None) -> None:
                    # Capturer le début avant le client, émettre après : le log ne doit pas
                    # gonfler le chrono du transport.
                    if first_call:
                        latency.emit(
                            timing,
                            "pre_first_telegram_call",
                            timing.acquired_at,
                            started_at,
                            result="calling",
                        )
                    latency.emit(
                        timing,
                        "transport_attempt",
                        started_at,
                        ended_at,
                        result=result,
                        transport=transport,
                        transport_attempt=number,
                    )

                try:
                    message = await operation
                except asyncio.CancelledError:
                    record_transport("cancelled", latency.clock())
                    diagnostic(DeliveryResult("unknown", transport, reason="cancelled"), number)
                    raise
                except Exception as exc:
                    # aiogram `check_response` traduit les 400/401/403/404 en ces types ;
                    # réseau, décodage, 5xx et exceptions inconnues ne prouvent pas un refus.
                    rejected = isinstance(
                        exc,
                        (
                            TelegramBadRequest,
                            TelegramUnauthorizedError,
                            TelegramForbiddenError,
                            TelegramNotFound,
                            TelegramRetryAfter,
                        ),
                    )
                    result = DeliveryResult(
                        "rejected" if rejected else "unknown",
                        transport,
                        reason=("photo_content_rejected" if transport == "photo"
                                and isinstance(exc, TelegramBadRequest)
                                and any(fragment in exc.message.lower() for fragment in (
                                    "wrong type of the web page content",
                                    "failed to get http url content", "photo_invalid",
                                )) else "api_rejected" if rejected else "client_result_unknown"),
                        error_type=type(exc).__name__,
                        retry_after=(exc.retry_after if isinstance(exc, TelegramRetryAfter)
                                     and type(exc.retry_after) is int
                                     and exc.retry_after > 0 else None),
                    )
                    record_transport(result.outcome, latency.clock())
                    diagnostic(result, number)
                    return result, True
                result = (
                    DeliveryResult("acknowledged", transport, receipt=message)
                    if usable_receipt(message, chat_id)
                    else DeliveryResult("unknown", transport, reason="unusable_receipt")
                )
                ended_at = latency.clock()
                record_transport(result.outcome, ended_at)
                if result:
                    timing.ack_at = ended_at
                    if timing.entered_at is not None:
                        latency.emit(
                            timing,
                            "entry_to_ack",
                            timing.entered_at,
                            ended_at,
                            result="acknowledged",
                            transport=transport,
                            message_id=message.message_id,
                        )
                diagnostic(result, number)
                return result, False

            result = DeliveryResult("unknown", "text")
            if photo_url is not None:
                for number in (1, 2):
                    result, raised = await request(
                        "photo",
                        bot.send_photo(
                            chat_id=chat_id,
                            photo=photo_url,
                            caption=caption,
                            reply_markup=keyboard,
                            request_timeout=10,
                        ),
                        number,
                    )
                    if result:
                        latency.safe_log(
                            logger,
                            logging.INFO,
                            "Résultat photo Telegram : "
                            + ("sent" if number == 1 else "retry_sent"),
                            {
                                "event": "photo_delivery",
                                "listing_id": str(listing.id),
                                "status": "sent" if number == 1 else "retry_sent",
                            },
                        )
                    else:
                        latency.safe_log(
                            logger,
                            logging.ERROR,
                            f"Échec {number}/2 de l'appel photo sans accusé exploitable.",
                            {},
                        )
                    if (result.outcome == "rejected"
                            and result.error_type == "TelegramBadRequest"
                            and result.reason != "photo_content_rejected"):
                        completed = True
                        return result
                    # Aucun nouvel essai photo quand le client revient sans exception,
                    # y compris sur le cas anormal None.
                    if result.outcome == "unknown":
                        completed = True
                        return result
                    if result.retry_after is not None or result.error_type in {
                        "TelegramUnauthorizedError", "TelegramForbiddenError", "TelegramNotFound",
                    }:
                        completed = True
                        return result
                    if not raised:
                        break
                    if number == 1:
                        pause_started = latency.clock()
                        pause_result = "waited"
                        try:
                            await asyncio.sleep(PHOTO_RETRY_DELAY_SECONDS)
                        except asyncio.CancelledError:
                            pause_result = "cancelled"
                            diagnostic(
                                DeliveryResult("unknown", "photo", reason="cancelled_before_retry"),
                                number,
                            )
                            raise
                        except BaseException:
                            pause_result = "error"
                            raise
                        finally:
                            latency.emit(
                                timing,
                                "retry_pause",
                                pause_started,
                                latency.clock(),
                                result=pause_result,
                                transport="photo",
                                requested_delay_ms=PHOTO_RETRY_DELAY_SECONDS * 1000,
                            )
                if not result:
                    latency.safe_log(
                        logger,
                        logging.ERROR,
                        "Aucun accusé photo exploitable ; repli sur message texte.",
                        {},
                    )
            if not result:
                text = caption + (
                    "\n\n<i>Photo indisponible ; l'annonce reste accessible via son lien.</i>"
                    if photo_url is not None
                    else ""
                )
                result, _ = await request(
                    "text",
                    bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=keyboard,
                        request_timeout=10,
                    ),
                    1,
                )
                latency.safe_log(
                    logger,
                    logging.INFO if result else logging.ERROR,
                    "Résultat du transport texte.",
                    {
                        "event": "photo_delivery",
                        "listing_id": str(listing.id),
                        "status": ("text_fallback" if photo_url else "text_only")
                        if result
                        else "failed",
                    },
                )

            if result and result.receipt is not None and settings.AI_ENABLED:
                enrichment = None
                try:
                    enrichment = ai_post_alert_pipeline.enrich_alert(
                        bot=bot,
                        chat_id=chat_id,
                        message_id=result.receipt.message_id,
                        listing=listing,
                        alert=alert,
                    )
                    safe_create_task(enrichment, name=f"ai_enrich_{listing.id}")
                except Exception as exc:
                    if enrichment is not None:
                        enrichment.close()
                    latency.safe_log(
                        logger,
                        logging.WARNING,
                        "Enrichissement non planifié après accusé Telegram.",
                        result.diagnostics(
                            alert_id=alert_id,
                            listing_id=listing.id,
                            attempt_id=attempt_id,
                            persistence_stage="not_started",
                            postprocessing_error=type(exc).__name__,
                        ),
                    )
            completed = True
            return result
        except BaseException as exc:
            terminal_result = "cancelled" if isinstance(exc, asyncio.CancelledError) else "unknown"
            terminal_error = type(exc).__name__
            raise
        finally:
            latency.emit(
                timing,
                "delivery_terminal",
                started_at,
                latency.clock(),
                result=result.outcome if completed and result is not None else terminal_result,
                entry_to_ack_ms=latency.elapsed(timing.entered_at, timing.ack_at)
                if completed and result is not None and result
                else None,
                error_type=terminal_error,
                message_id=result.receipt.message_id
                if completed and result is not None and result.receipt is not None
                else None,
            )


async def notify_admins(
    text: str,
    bot: Bot | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Diffuse un message d'état système à tous les utilisateurs Telegram autorisés.

    Le client par défaut est l'instance partagée du bot : en créer un à chaque
    notification ouvrait une nouvelle session réseau pour un envoi, alors que le
    cycle de vie du client est déjà géré par le processus.

    Rend `True` seulement si **tous** les envois ont abouti : un appelant qui
    journalise ce booléen ne doit jamais enregistrer un succès fictif.
    """
    allowed_ids = settings.TELEGRAM_ALLOWED_USER_IDS
    if not allowed_ids:
        return False

    target_bot = bot
    if target_bot is None:
        from app.bot.bot_instance import bot as default_bot

        target_bot = default_bot

    delivered = True
    for user_id in allowed_ids:
        try:
            call: dict[str, Any] = {
                "chat_id": user_id,
                "text": text,
                "parse_mode": "HTML",
            }
            if reply_markup is not None:
                call["reply_markup"] = reply_markup
            await target_bot.send_message(**call)
        except Exception as exc:
            delivered = False
            logger.warning(
                "Impossible d'envoyer la notification système à l'utilisateur %s: %s",
                user_id,
                exc,
            )
    return delivered
