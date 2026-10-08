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
    assert "<b>État :</b> TBE" in caption
    assert "<b>Prix :</b> 9,00 € (13,69 € TTC)" in caption
    assert "Occasion :" not in caption


def test_telegram_caption_bd_with_market_price_and_keywords():
    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    comparison = "<b>Prix du marché :</b> 22,00 € livré\n<b>Écart :</b> -8,31 € ✅"
    caption = build_listing_caption(
        listing,
        market_comparison_line=comparison,
        alert_keywords=["blacksad", "intégrale"],
    )

    assert "🆕 <b>Blacksad — Intégrale</b>" in caption
    assert "<b>État :</b> TBE" in caption
    assert "<b>Prix :</b> 9,00 € (13,69 € TTC)" in caption
    assert "<b>Prix du marché :</b> 22,00 € livré" in caption
    assert "<b>Écart :</b> -8,31 € ✅" in caption
    assert "<b>Mots-clés :</b> blacksad, intégrale" in caption


def test_telegram_caption_clothes():
    listing = create_sample_listing(
        category_slug=CategorySlug.CLOTHES.value,
        title="Veste Carhartt Detroit",
        item_price_cents=4500,
        estimated_total_cents=5100,
    )
    caption = build_listing_caption(listing)

    assert "🆕 <b>Veste Carhartt Detroit</b>" in caption
    assert "<b>État :</b> TBE" in caption
    assert "<b>Prix :</b> 45,00 € (51,00 € TTC)" in caption
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
    assert "<b>État :</b> TBE" in caption
    assert "<b>Prix :</b> 60,00 € (66,58 € TTC)" in caption
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

    assert rows[1][0].text == "Prix du marché"
    assert rows[1][0].callback_data == f"market:{listing_id}"

    assert rows[2][0].text == "Faux positif"
    assert rows[2][0].callback_data == f"dislike:{listing_id}"
    assert rows[2][1].text == "Négocier"
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

        assert rows[1][0].text == "Faux positif"
        assert rows[1][0].callback_data == f"dislike:{listing_id}"
        assert rows[1][1].text == "Négocier"
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
    assert rows[1][0].text == "Prix du marché"
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
    assert kb.inline_keyboard[2][0].text == "Faux positif enregistré"
    assert kb.inline_keyboard[2][0].callback_data == f"disliked:{listing_id}"


def _make_alert_message() -> MagicMock:
    """Le message d'alerte tel que l'utilisateur l'a reçu : légende + clavier."""
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock()
    message.edit_reply_markup = AsyncMock()
    message.reply = AsyncMock()
    # `delete` est bien une méthode de Message, mais pas un champ du modèle
    # pydantic : spec=Message ne l'expose pas, il faut la poser explicitement.
    message.delete = AsyncMock()
    return message


def _press(data: str, message: MagicMock) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = message
    callback.from_user = MagicMock(id=1)
    callback.bot = MagicMock()
    return callback


async def _press_dislike(listing: Listing) -> MagicMock:
    """Joue l'appui sur « Faux positif » et rend le message d'annonce."""

    alert_message = _make_alert_message()
    with patch(
        "app.bot.handlers.callbacks.ListingRepository.get_by_id",
        new_callable=AsyncMock,
        return_value=listing,
    ):
        await handle_dislike(_press(f"dislike:{listing.id}", alert_message), MagicMock())
    return alert_message


@pytest.mark.asyncio
async def test_dislike_confirmation_says_what_confirm_confirms():
    """La confirmation est le clavier du message d'annonce, sans nouveau message."""
    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    message = await _press_dislike(listing)
    message.reply.assert_not_awaited()
    rows = message.edit_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard
    assert [button.text for button in rows[-1]] == ["Confirmer", "Annuler"]


@pytest.mark.asyncio
async def test_dislike_confirmation_escapes_the_listing_title():
    """Un titre HTML reste intact : aucun texte n'est reconstruit à la confirmation."""
    listing = create_sample_listing(
        category_slug=CategorySlug.BD_COMICS.value, title="<b>BD</b> & cie"
    )
    message = await _press_dislike(listing)
    message.reply.assert_not_awaited()
    message.edit_text.assert_not_awaited()
    message.edit_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_dislike_never_modifies_the_announcement_message():
    """L'annonce porte le prix et la cote : elle ne doit jamais être réécrite.

    La légende n'est pas reconstructible ici : seuls les boutons changent,
    le prix et la cote restent exactement ceux reçus à l'ingestion.
    """
    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    alert_message = await _press_dislike(listing)

    alert_message.edit_text.assert_not_awaited()
    alert_message.edit_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_dislike_discards_the_confirmation_only():
    """Annuler restaure les actions sur le même message, sans suppression."""
    from app.bot.handlers.callbacks import handle_cancel_dislike

    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    message = _make_alert_message()
    with patch(
        "app.bot.handlers.callbacks.ListingRepository.get_by_id", AsyncMock(return_value=listing)
    ):
        await handle_cancel_dislike(_press(f"cancel_dislike:{listing.id}", message), MagicMock())
    message.delete.assert_not_awaited()
    message.edit_text.assert_not_awaited()
    rows = message.edit_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard
    assert rows[-1][0].callback_data == f"dislike:{listing.id}"


@pytest.mark.asyncio
async def test_confirm_dislike_discards_the_confirmation_only():
    """« Confirmer » ferme l'écran de confirmation ; l'annonce reste intacte."""
    from app.bot.handlers.callbacks import handle_confirm_dislike

    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    await _press_dislike(listing)

    confirmation = _make_alert_message()
    db_session = MagicMock()
    db_session.commit = AsyncMock()
    with (
        patch(
            "app.bot.handlers.callbacks.ListingRepository.get_by_id",
            new_callable=AsyncMock,
            return_value=listing,
        ),
        patch(
            "app.bot.handlers.callbacks.ListingRepository.get_alert_for_listing",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("app.bot.handlers.callbacks.record_listing_dislike", new_callable=AsyncMock),
    ):
        await handle_confirm_dislike(
            _press(f"confirm_dislike:{listing.id}", confirmation), db_session
        )

    confirmation.delete.assert_not_awaited()
    rows = confirmation.edit_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard
    assert rows[-1][0].callback_data == f"disliked:{listing.id}"
    confirmation.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_dislike_confirmation_still_toasts():
    """Le toast reste : il confirme que le clic a été pris en compte."""

    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    alert_message = _make_alert_message()
    callback = _press(f"dislike:{listing.id}", alert_message)
    with patch(
        "app.bot.handlers.callbacks.ListingRepository.get_by_id",
        new_callable=AsyncMock,
        return_value=listing,
    ):
        await handle_dislike(callback, MagicMock())

    callback.answer.assert_awaited_once_with("Confirmer le faux positif ?")


@pytest.mark.asyncio
async def test_dislike_confirmation_survives_a_reply_failure():
    """Si l'envoi du message de confirmation échoue, le clic ne doit pas lever.

    Le message d'annonce reste intact dans tous les cas : l'utilisateur peut
    rejouer l'appui plutôt que de rester sur un écran incohérent.
    """

    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    alert_message = _make_alert_message()
    alert_message.edit_reply_markup = AsyncMock(
        side_effect=TelegramBadRequest(
            method=EditMessageText(chat_id=1, message_id=1, text="x"),
            message="Bad Request: chat not found",
        )
    )
    callback = _press(f"dislike:{listing.id}", alert_message)

    with patch(
        "app.bot.handlers.callbacks.ListingRepository.get_by_id",
        new_callable=AsyncMock,
        return_value=listing,
    ):
        await handle_dislike(callback, MagicMock())

    callback.answer.assert_awaited_once()
