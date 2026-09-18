"""Tests unitaires pour VisionGuardService (détection de boîtes vides / arnaques)."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.listing import Listing
from app.services.vision_guard_service import VisionGuardService


def test_vision_guard_empty_box_detection():
    # Annonce suspecte contenant "boite vide"
    listing_suspicious = Listing(
        id=uuid.uuid4(),
        category_slug="tech",
        title="Boite vide Mario Kart 8 Deluxe Switch",
        description="Juste la boite sans le jeu",
        item_price_cents=500,
    )
    res = VisionGuardService.analyze_listing(listing_suspicious)
    assert res.is_suspicious is True
    assert "Mention suspecte" in res.reason or "boite vide" in res.reason


def test_vision_guard_normal_game():
    listing_normal = Listing(
        id=uuid.uuid4(),
        category_slug="tech",
        title="Mario Kart 8 Deluxe Switch complet",
        description="Jeu en très bon état avec sa cartouche",
        item_price_cents=2500,
    )
    res = VisionGuardService.analyze_listing(listing_normal)
    assert res.is_suspicious is False


@pytest.mark.asyncio
async def test_vision_guard_check_and_notify_sends_warning():
    bot = MagicMock()
    bot.send_message = AsyncMock()

    listing_fake = Listing(
        id=uuid.uuid4(),
        category_slug="tech",
        title="Boitier seul Zelda BOTW",
        description="vendu sans cartouche",
        item_price_cents=400,
    )

    await VisionGuardService.check_and_notify(
        bot=bot,
        chat_id=12345,
        message_id=999,
        listing=listing_fake,
    )

    bot.send_message.assert_called_once()
    args, kwargs = bot.send_message.call_args
    assert kwargs["reply_to_message_id"] == 999
    assert "ATTENTION ARNAQUE SUSPECTÉE" in kwargs["text"]


def test_vision_guard_book_trap_detection():
    listing_fourreau = Listing(
        id=uuid.uuid4(),
        category_slug="bd_comics",
        title="Fourreau seul Blacksad",
        description="Attention vendu sans le livre",
        item_price_cents=1000,
    )
    res = VisionGuardService.analyze_listing(listing_fourreau)
    assert res.is_suspicious is True
    assert "Mention suspecte" in res.reason or "fourreau" in res.reason
