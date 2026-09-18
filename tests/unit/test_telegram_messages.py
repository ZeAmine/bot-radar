"""Tests unitaires pour le format des messages et boutons Telegram d'annonces."""

import uuid

from app.bot.keyboards.listings import get_listing_alert_keyboard
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
    assert "💎 Très bon état" in caption
    assert "💰 Prix : 9,00 € | 13,69 € (TTC)" in caption
    assert "📉 Occasion : indisponible" in caption


def test_telegram_caption_bd_with_market_price_and_keywords():
    listing = create_sample_listing(category_slug=CategorySlug.BD_COMICS.value)
    comparison = "📉 Occasion : 22,00 € livré\n✅ Écart : -8,31 €"
    caption = build_listing_caption(
        listing,
        market_comparison_line=comparison,
        alert_keywords=["blacksad", "intégrale"],
    )

    assert "🆕 <b>Blacksad — Intégrale</b>" in caption
    assert "💎 Très bon état" in caption
    assert "💰 Prix : 9,00 € | 13,69 € (TTC)" in caption
    assert "📉 Occasion : 22,00 € livré" in caption
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
    assert "💎 Très bon état" in caption
    assert "💰 Prix : 45,00 € | 51,00 € (TTC)" in caption
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
    assert "💎 Très bon état" in caption
    assert "💰 Prix : 60,00 € | 66,58 € (TTC)" in caption
    assert "Occasion :" not in caption


def test_telegram_buttons_bd_comics():
    listing_id = uuid.uuid4()
    url = "https://www.example.com/listing-bd"
    keyboard = get_listing_alert_keyboard(
        listing_id,
        url,
        category_slug=CategorySlug.BD_COMICS.value,
    )

    # 2 lignes : photos/vinted, puis payer / prix du marché
    rows = keyboard.inline_keyboard
    assert len(rows) == 2
    assert len(rows[0]) == 2
    assert len(rows[1]) == 2

    assert rows[0][0].text == "🖼 Voir les photos"
    assert rows[0][0].callback_data == f"photos:{listing_id}"

    assert rows[0][1].text == "🔗 Voir Vinted"
    assert rows[0][1].url == url

    assert rows[1][0].text == "🛍 Payer"
    assert rows[1][0].url == f"{url}#radar_autobuy=1"

    assert rows[1][1].text == "📉 Prix du marché"
    assert rows[1][1].callback_data == f"market:{listing_id}"


def test_telegram_buttons_clothes_and_tech():
    listing_id = uuid.uuid4()
    url = "https://www.vinted.fr/items/54321987-nintendo-switch"

    for cat in [CategorySlug.CLOTHES.value, CategorySlug.TECH.value]:
        keyboard = get_listing_alert_keyboard(listing_id, url, category_slug=cat)
        rows = keyboard.inline_keyboard
        assert len(rows) == 2
        assert len(rows[0]) == 2
        assert len(rows[1]) == 1

        assert rows[0][0].text == "🖼 Voir les photos"
        assert rows[0][0].callback_data == f"photos:{listing_id}"
        assert rows[0][1].text == "🔗 Voir Vinted"
        assert rows[0][1].url == url

        assert rows[1][0].text == "🛍 Payer"
        assert rows[1][0].url == f"{url}#radar_autobuy=1"
