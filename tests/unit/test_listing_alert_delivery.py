"""Verrous de l'envoi de l'alerte d'annonce : photo tentée deux fois, repli nommé.

Incident du 2026-10-06 : Telegram a refusé l'URL de couverture (« wrong type of the
web page content » — le CDN avait renvoyé autre chose qu'une image au téléchargement),
l'exception était avalée par un simple `logger.warning`, et l'alerte est partie en
texte : ni image, ni erreur, annonce marquée notifiée.

Ces tests tiennent le correctif là où la panne s'est produite : `send_listing_alert`.
La photo est reprise une fois avant tout repli, chaque dégradation est journalisée à
ERROR, et les cas limites (image absente, URL impropre, légende trop longue) sont
nommés plutôt que tués en silence. Le repli texte reste garanti : une panne photo ne
fait jamais perdre l'alerte.
"""

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.core.config import settings
from app.core.constants import CategorySlug
from app.db.models.listing import Listing
from app.services.telegram_service import PHOTO_CAPTION_LIMIT, send_listing_alert
from tests.telegram_fakes import telegram_message

CHAT_ID = 6429948805
PHOTO_URL = "https://images1.vinted.net/t/02_0185e_AhDBMrrRQHGNdMPBKvDGus8k/f800/bf60c809.webp?s=41210d6a"
LOGGER_NAME = "app.services.telegram_service"


def _listing(*, title: str = "Cauchon ou l'homme qui a tué Jeanne d'Arc", photos=None) -> Listing:
    """Annonce telle que l'incident du 2026-10-06 l'a ingérée : couverture présente."""
    return Listing(
        id=uuid.uuid4(),
        source_name="vinted",
        external_id=str(uuid.uuid4()),
        url="https://www.vinted.fr/items/1",
        title=title,
        normalized_title="cauchon",
        category_slug=CategorySlug.BD_COMICS.value,
        item_price_cents=2000,
        estimated_total_cents=2458,
        condition="Très bon état",
        raw_payload={},
        photos=[PHOTO_URL] if photos is None else photos,
    )


def _bot(*, photo_side_effect=None):
    """Bot doublon : la photo échoue comme Telegram l'a fait, le message part toujours."""
    bot = MagicMock()
    bot.send_photo = AsyncMock(side_effect=photo_side_effect)
    bot.send_message = AsyncMock(return_value=telegram_message(CHAT_ID, 2))
    return bot


def _telegram_refusal() -> TelegramBadRequest:
    return TelegramBadRequest(
        method=None,
        message="Bad Request: wrong type of the web page content",
    )


@pytest.mark.asyncio
async def test_photo_sent_on_the_first_attempt_without_touching_the_text(monkeypatch):
    """Chemin nominal inchangé : une photo acceptée part en légende, jamais en texte."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    bot = _bot(photo_side_effect=None)
    bot.send_photo.return_value = telegram_message(CHAT_ID, 1)

    sent = await send_listing_alert(bot, CHAT_ID, _listing())

    assert sent.outcome == "acknowledged"
    assert sent.receipt is not None
    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["photo"] == PHOTO_URL
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_refused_by_telegram_is_attempted_once_more(monkeypatch):
    """Un refus de téléchargement vaut une seconde tentative — pas un repli immédiat.

    C'est la panne exacte du 2026-10-06 : le CDN renvoie autre chose qu'une image au
    téléchargement de Telegram, et le second essai, lui, réussit.
    """
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    bot = _bot(photo_side_effect=[_telegram_refusal(), telegram_message(CHAT_ID, 1)])

    with patch("asyncio.sleep", new_callable=AsyncMock) as pause:
        sent = await send_listing_alert(bot, CHAT_ID, _listing())

    assert sent.outcome == "acknowledged"
    assert sent.receipt is not None
    assert bot.send_photo.await_count == 2
    pause.assert_awaited_once()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_alert_falls_back_to_text_after_two_failures_and_says_so(
    monkeypatch, caplog
):
    """Deux échecs : le texte part, et l'anomalie est une erreur, pas un avertissement."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    bot = _bot(photo_side_effect=_telegram_refusal())

    with (
        patch("asyncio.sleep", new_callable=AsyncMock),
        caplog.at_level(logging.ERROR, logger=LOGGER_NAME),
    ):
        sent = await send_listing_alert(bot, CHAT_ID, _listing())

    assert sent.outcome == "acknowledged"
    assert sent.receipt is not None
    assert bot.send_photo.await_count == 2
    bot.send_message.assert_awaited_once()
    assert bot.send_message.call_args.kwargs["text"].startswith("🆕 <b>Cauchon")
    assert "Photo indisponible" in bot.send_message.call_args.kwargs["text"]

    messages = [record.getMessage() for record in caplog.records]
    assert any("Échec 1/2" in message for message in messages)
    assert any("Échec 2/2" in message for message in messages)
    assert any("repli sur message texte" in message for message in messages)


@pytest.mark.asyncio
async def test_missing_photo_goes_straight_to_text_and_is_traceable(monkeypatch, caplog):
    """Image absente : le texte part, et l'absence est journalisée — plus de silence."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    bot = _bot()

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        sent = await send_listing_alert(bot, CHAT_ID, _listing(photos=[]))

    assert sent.outcome == "acknowledged"
    assert sent.receipt is not None
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    assert any("sans photo" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_photo_url_not_https_never_reaches_telegram(monkeypatch, caplog):
    """Format invalide : aucune tentative d'envoi, le texte porte l'alerte."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    bot = _bot()

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        sent = await send_listing_alert(
            bot, CHAT_ID, _listing(photos=["http://images.vinted.fr/cover.jpg"])
        )

    assert sent.outcome == "acknowledged"
    assert sent.receipt is not None
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    assert any("URL de photo invalide" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_caption_over_the_photo_limit_keeps_the_whole_text(monkeypatch, caplog):
    """Légende > 1024 : la photo serait refusée pour elle — le texte, lui, tient (4096)."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    bot = _bot()
    long_title = "Cauchon " * 140  # bien au-delà de la limite de légende

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        sent = await send_listing_alert(bot, CHAT_ID, _listing(title=long_title))

    assert sent.outcome == "acknowledged"
    assert sent.receipt is not None
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    caption = bot.send_message.call_args.kwargs["text"]
    assert len(caption) > PHOTO_CAPTION_LIMIT
    assert long_title.strip() in caption
    assert any("légende de" in record.getMessage() for record in caplog.records)
