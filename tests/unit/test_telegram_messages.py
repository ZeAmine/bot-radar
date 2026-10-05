"""Tests unitaires pour le format des messages et boutons Telegram d'annonces."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import Message

from app.bot.handlers.callbacks import handle_dislike
from app.bot.keyboards.listings import (
    get_dislike_confirmation_keyboard,
    get_listing_alert_keyboard,
)
from app.core.constants import CategorySlug
from app.db.models.listing import Listing
from app.services.telegram_service import build_listing_caption


def create_sample_listing(
    *,
    category_slug: str,
    title: str = "Blacksad — Intégrale",
    condition: str = "Très bon état",
    item_price_cents: int = 900,
    estimated_total_cents: int | None = 1369,
) -> Listing:
    return Listing(
        id=uuid.uuid4(),
        source_name="authorized-source",
        external_id=str(uuid.uuid4()),
        url="https://www.example.com/listing-1",
        title=title,
        normalized_title=title.lower(),
        category_slug=category_slug,
        item_price_cents=item_price_cents,
        estimated_total_cents=estimated_total_cents,
        condition=condition,
        raw_payload={},
    )


def test_telegram_caption_bd_without_market_price():
    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    caption = build_listing_caption(listing, market_comparison_line=None)

    assert "🆕 <b>Blacksad — Intégrale</b>" in caption
    assert "État : TBE" in caption
    assert "Prix : 9,00 € (Total est. : 13,69 €)" in caption
    assert "Occasion :" not in caption


def test_telegram_caption_bd_with_market_price_and_keywords():
    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    comparison = "Prix du marché : 22,00 € livré\n✅ Écart : -8,31 €"
    caption = build_listing_caption(
        listing,
        market_comparison_line=comparison,
        alert_keywords=["blacksad", "intégrale"],
    )

    assert "🆕 <b>Blacksad — Intégrale</b>" in caption
    assert "État : TBE" in caption
    assert "Prix : 9,00 € (Total est. : 13,69 €)" in caption
    assert "Prix du marché : 22,00 € livré" in caption
    assert "✅ Écart : -8,31 €" in caption
    assert "Mots-clés : blacksad, intégrale" in caption


def test_telegram_caption_clothes():
    listing = create_sample_listing(
        category_slug=CategorySlug.CLOTHES.value,
        title="Veste Carhartt Detroit",
        item_price_cents=4500,
        estimated_total_cents=5100,
    )
    caption = build_listing_caption(listing)

    assert "🆕 <b>Veste Carhartt Detroit</b>" in caption
    assert "État : TBE" in caption
    assert "Prix : 45,00 € (Total est. : 51,00 €)" in caption
    # Jamais de prix marché pour les vêtements
    assert "Occasion :" not in caption


def test_telegram_caption_tech():
    listing = create_sample_listing(
        category_slug=CategorySlug.TECH.value,
        title="Logitech MX Master 3S",
        item_price_cents=6000,
        # Frais par défaut Vinted (60,00 + 3,70 + 2,88 = 66,58 €)
        estimated_total_cents=None,
    )
    caption = build_listing_caption(listing)

    assert "🆕 <b>Logitech MX Master 3S</b>" in caption
    assert "État : TBE" in caption
    assert "Prix : 60,00 € (Total est. : 66,58 €)" in caption
    assert "Occasion :" not in caption


def test_telegram_buttons_bd_comics():
    listing_id = uuid.uuid4()
    url = "https://www.vinted.fr/items/123456-listing-bd"
    keyboard = get_listing_alert_keyboard(
        listing_id,
        url,
        category_slug=CategorySlug.BD_COMICS.value,
    )

    # 3 lignes : annonce, prix du marché, dislike / négocier
    rows = keyboard.inline_keyboard
    assert len(rows) == 3
    assert len(rows[0]) == 1
    assert len(rows[1]) == 1
    assert len(rows[2]) == 2

    assert rows[0][0].text == "Voir l'annonce"
    assert rows[0][0].url == url

    assert rows[1][0].text == "📊 Prix du marché"
    assert rows[1][0].callback_data == f"market:{listing_id}"

    assert rows[2][0].text == "👎 Faux positif"
    assert rows[2][0].callback_data == f"dislike:{listing_id}"
    assert rows[2][1].text == "💬 Négocier"
    assert rows[2][1].callback_data == f"negotiate:{listing_id}"

    # Vérifie l'absence absolue de bouton Payer et Dressing
    all_texts = [btn.text for row in rows for btn in row]
    assert not any("payer" in t.lower() for t in all_texts)
    assert not any("dressing" in t.lower() for t in all_texts)


def test_telegram_buttons_clothes_and_tech():
    listing_id = uuid.uuid4()
    url = "https://www.vinted.fr/items/54321987-nintendo-switch"

    for cat in [CategorySlug.CLOTHES.value, CategorySlug.TECH.value]:
        keyboard = get_listing_alert_keyboard(listing_id, url, category_slug=cat)
        rows = keyboard.inline_keyboard
        # 2 lignes : annonce, dislike / négocier
        assert len(rows) == 2
        assert len(rows[0]) == 1
        assert len(rows[1]) == 2

        assert rows[0][0].text == "Voir l'annonce"
        assert rows[0][0].url == url

        assert rows[1][0].text == "👎 Faux positif"
        assert rows[1][0].callback_data == f"dislike:{listing_id}"
        assert rows[1][1].text == "💬 Négocier"
        assert rows[1][1].callback_data == f"negotiate:{listing_id}"

        all_texts = [btn.text for row in rows for btn in row]
        assert not any("payer" in t.lower() for t in all_texts)
        assert not any("dressing" in t.lower() for t in all_texts)


def test_telegram_buttons_no_payer_button():
    """Confirme qu'aucun bouton de paiement direct n'est généré sur toutes les catégories."""
    listing_id = uuid.uuid4()
    url = "https://www.vinted.fr/items/99999-item"

    for slug in [
        CategorySlug.BD_COMICS.value,
        CategorySlug.TECH.value,
        CategorySlug.GAMING.value,
        CategorySlug.CLOTHES.value,
    ]:
        kb = get_listing_alert_keyboard(listing_id, url, category_slug=slug)
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        assert not any("payer" in b.text.lower() for b in all_buttons)
        assert not any(b.url and "transaction/buy" in b.url for b in all_buttons)


def test_dislike_confirmation_keyboard():
    listing_id = uuid.uuid4()
    url = "https://www.vinted.fr/items/123456-listing-bd"
    kb = get_dislike_confirmation_keyboard(
        listing_id=listing_id,
        listing_url=url,
        category_slug=CategorySlug.BD_COMICS.value,
    )
    rows = kb.inline_keyboard
    assert len(rows) == 3
    assert rows[0][0].text == "Voir l'annonce"
    assert rows[1][0].text == "📊 Prix du marché"
    # Ligne 3 = confirmation et annulation
    assert rows[2][0].text == "Confirmer"
    assert rows[2][0].callback_data == f"confirm_dislike:{listing_id}"
    assert rows[2][1].text == "Annuler"
    assert rows[2][1].callback_data == f"cancel_dislike:{listing_id}"

    all_texts = [btn.text for row in rows for btn in row]
    assert not any("payer" in t.lower() for t in all_texts)


def test_disliked_button_text():
    listing_id = uuid.uuid4()
    url = "https://www.vinted.fr/items/123456-listing-bd"
    kb = get_listing_alert_keyboard(
        listing_id=listing_id,
        listing_url=url,
        category_slug=CategorySlug.BD_COMICS.value,
        is_disliked=True,
    )
    # Ligne 3, bouton 1 : doit être '✅ Signalé'
    assert kb.inline_keyboard[2][0].text == "✅ Signalé"
    assert kb.inline_keyboard[2][0].callback_data == f"disliked:{listing_id}"




def _make_callback(listing_id: uuid.UUID) -> tuple[MagicMock, MagicMock]:
    """Callback « Faux positif » avec un message d'alerte réel comme support."""
    callback = MagicMock()
    callback.data = f"dislike:{listing_id}"
    callback.answer = AsyncMock()
    # spec=Message : le handler filtre sur isinstance(callback.message, Message),
    # un MagicMock nu n'y passe pas et le chemin testé ne serait jamais pris.
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    message.edit_reply_markup = AsyncMock()
    callback.message = message
    return callback, message


async def _press_dislike(listing: Listing) -> tuple[MagicMock, MagicMock]:
    """Joue l'appui sur « Faux positif » jusqu'au clavier de confirmation."""
    callback, message = _make_callback(listing.id)
    with patch(
        "app.bot.handlers.callbacks.ListingRepository.get_by_id",
        new_callable=AsyncMock,
        return_value=listing,
    ):
        await handle_dislike(callback, MagicMock())
    return callback, message


@pytest.mark.asyncio
async def test_dislike_confirmation_says_what_confirm_confirms():
    """« Confirmer » doit dire ce qu'il confirme, pas seulement clignoter un toast.

    Le clavier remplace celui de l'alerte *sur le message existant*, sans texte
    nouveau : sous une légende d'annonce, un « Confirmer » nu se lit comme une
    confirmation d'achat. Le toast ne suffit pas — il disparaît en quelques
    secondes alors que le clavier reste affiché.
    """
    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    _callback, message = await _press_dislike(listing)

    message.edit_text.assert_awaited_once()
    kwargs = message.edit_text.call_args.kwargs
    text = kwargs["text"]

    assert "faux positif" in text.lower()
    assert listing.title in text
    assert "Confirmer" in [b.text for b in kwargs["reply_markup"].inline_keyboard[-1]]


@pytest.mark.asyncio
async def test_dislike_confirmation_escapes_the_listing_title():
    """Un titre contenant du HTML ne doit pas casser la mise en forme du message."""
    listing = create_sample_listing(
        category_slug=CategorySlug.BD_COMICS.value, title="<b>BD</b> & cie")
    _callback, message = await _press_dislike(listing)

    text = message.edit_text.call_args.kwargs["text"]
    assert "<b>BD</b>" not in text
    assert "&lt;b&gt;BD&lt;/b&gt; &amp; cie" in text


@pytest.mark.asyncio
async def test_dislike_confirmation_keeps_the_keyboard_when_text_edit_fails():
    """Si l'édition du texte échoue, le clavier doit quand même être proposé."""
    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    callback, message = _make_callback(listing.id)
    # Telegram refuse editMessageText quand le texte est identique (« message is
    # not modified ») : c'est le cas réel qui justifie le repli, pas une panne rare.
    message.edit_text = AsyncMock(side_effect=TelegramBadRequest(
        method=EditMessageText(chat_id=1, message_id=1, text="x"),
        message="Bad Request: message is not modified",
    ))

    with patch(
        "app.bot.handlers.callbacks.ListingRepository.get_by_id",
        new_callable=AsyncMock,
        return_value=listing,
    ):
        await handle_dislike(callback, MagicMock())

    message.edit_reply_markup.assert_awaited_once()
    keyboard = message.edit_reply_markup.call_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[-1][0].text == "Confirmer"


@pytest.mark.asyncio
async def test_dislike_confirmation_still_toasts():
    """Le toast reste : il confirme que le clic a été pris en compte."""
    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    callback, _message = await _press_dislike(listing)

    callback.answer.assert_awaited_once_with("Confirmer le faux positif ?")
