"""Tests unitaires pour le format des messages et boutons Telegram d'annonces."""

import uuid

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
    assert "🏷 État : Très bon état" in caption
    assert "💵 Prix : 9,00 € (13,69 € TTC)" in caption
    assert "Occasion :" not in caption


def test_telegram_caption_bd_with_market_price_and_keywords():
    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    comparison = "📊 Prix du marché : 22,00 € livré\n✅ Écart : -8,31 €"
    caption = build_listing_caption(
        listing,
        market_comparison_line=comparison,
        alert_keywords=["blacksad", "intégrale"],
    )

    assert "🆕 <b>Blacksad — Intégrale</b>" in caption
    assert "🏷 État : Très bon état" in caption
    assert "💵 Prix : 9,00 € (13,69 € TTC)" in caption
    assert "📊 Prix du marché : 22,00 € livré" in caption
    assert "✅ Écart : -8,31 €" in caption
    assert "🔍 Mots-clés : blacksad, intégrale" in caption


def test_telegram_caption_clothes():
    listing = create_sample_listing(
        category_slug=CategorySlug.CLOTHES.value,
        title="Veste Carhartt Detroit",
        item_price_cents=4500,
        estimated_total_cents=5100,
    )
    caption = build_listing_caption(listing)

    assert "🆕 <b>Veste Carhartt Detroit</b>" in caption
    assert "🏷 État : Très bon état" in caption
    assert "💵 Prix : 45,00 € (51,00 € TTC)" in caption
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
    assert "🏷 État : Très bon état" in caption
    assert "💵 Prix : 60,00 € (66,58 € TTC)" in caption
    assert "Occasion :" not in caption


def test_telegram_buttons_bd_comics():
    listing_id = uuid.uuid4()
    url = "https://www.vinted.fr/items/123456-listing-bd"
    keyboard = get_listing_alert_keyboard(
        listing_id,
        url,
        category_slug=CategorySlug.BD_COMICS.value,
    )

    # 4 lignes : photos/vinted, prix du marché / payer, dislike / négocier, puis dressing / lot
    rows = keyboard.inline_keyboard
    assert len(rows) == 4
    assert len(rows[0]) == 2
    assert len(rows[1]) == 2
    assert len(rows[2]) == 2
    assert len(rows[3]) == 1

    assert rows[0][0].text == "🖼 Photos"
    assert rows[0][0].callback_data == f"photos:{listing_id}"

    assert rows[0][1].text == "🔗 Annonce"
    assert rows[0][1].url == url

    assert rows[1][0].text == "📊 Prix du marché"
    assert rows[1][0].callback_data == f"market:{listing_id}"

    assert rows[1][1].text == "💳 Payer"
    assert rows[1][1].url == "https://www.vinted.fr/transaction/buy/new?transaction[item_id]=123456"

    assert rows[2][0].text == "👎 Faux positif"
    assert rows[2][0].callback_data == f"dislike:{listing_id}"
    assert rows[2][1].text == "💬 Négocier"
    assert rows[2][1].callback_data == f"negotiate:{listing_id}"

    assert rows[3][0].text == "🛍️ Dressing / Lot (bientôt)"
    assert rows[3][0].callback_data == "bundle_dressing_disabled"


def test_telegram_buttons_clothes_and_tech():
    listing_id = uuid.uuid4()
    url = "https://www.vinted.fr/items/54321987-nintendo-switch"

    for cat in [CategorySlug.CLOTHES.value, CategorySlug.TECH.value]:
        keyboard = get_listing_alert_keyboard(listing_id, url, category_slug=cat)
        rows = keyboard.inline_keyboard
        assert len(rows) == 4
        assert len(rows[0]) == 2
        assert len(rows[1]) == 1
        assert len(rows[2]) == 2
        assert len(rows[3]) == 1

        assert rows[0][0].text == "🖼 Photos"
        assert rows[0][0].callback_data == f"photos:{listing_id}"
        assert rows[0][1].text == "🔗 Annonce"
        assert rows[0][1].url == url

        assert rows[1][0].text == "💳 Payer"
        assert rows[1][0].url == "https://www.vinted.fr/transaction/buy/new?transaction[item_id]=54321987"

        assert rows[2][0].text == "👎 Faux positif"
        assert rows[2][0].callback_data == f"dislike:{listing_id}"
        assert rows[2][1].text == "💬 Négocier"
        assert rows[2][1].callback_data == f"negotiate:{listing_id}"

        assert rows[3][0].text == "🛍️ Dressing / Lot (bientôt)"
        assert rows[3][0].callback_data == "bundle_dressing_disabled"


def test_telegram_buttons_with_explicit_checkout_or_external_id():
    listing_id = uuid.uuid4()
    url = "https://www.vinted.fr/items/99999-item"

    # Test avec checkout_url explicite
    custom_checkout = "https://www.vinted.fr/transaction/buy/new?transaction[item_id]=custom123"
    kb1 = get_listing_alert_keyboard(
        listing_id,
        url,
        category_slug=CategorySlug.TECH.value,
        checkout_url=custom_checkout,
    )
    assert kb1.inline_keyboard[1][0].url == custom_checkout

    # Test avec external_id explicite
    kb2 = get_listing_alert_keyboard(
        listing_id,
        url,
        category_slug=CategorySlug.TECH.value,
        external_id="88888",
    )
    assert (
        kb2.inline_keyboard[1][0].url
        == "https://www.vinted.fr/transaction/buy/new?transaction[item_id]=88888"
    )


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
    # Ligne 3 = confirmation et annulation
    assert rows[2][0].text == "⚠️ Confirmer ?"
    assert rows[2][0].callback_data == f"confirm_dislike:{listing_id}"
    assert rows[2][1].text == "❌ Annuler"
    assert rows[2][1].callback_data == f"cancel_dislike:{listing_id}"


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


