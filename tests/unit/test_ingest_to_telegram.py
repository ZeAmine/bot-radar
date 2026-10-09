"""Parcours d'ingestion : du scraper Vinted au message remis à Telegram.

Le chemin réel est traversé de bout en bout, sans le harnais `/add` : le scraper
Vinted reçoit une page de catalogue **simulée**, la parse pour de vrai
(`fetch_vinted_page` → `parse_vinted_catalog_json`), `to_ingest_payload` fabrique la
charge, et cette charge est postée sur `POST /api/v1/ingest/listings` →
`ProcessListingUseCase` → `evaluate_listing_against_alert` (la couture qui porte les
mots-clés et les variantes) → `send_listing_alert` → `build_listing_caption` →
`Bot.send_photo`/`Bot.send_message`.

C'est la même marche que `scripts/local_vinted_poller.py`, le seul vrai appelant de
l'endpoint : fetch, conversion, POST. Ce que le test faux, ce sont les deux frontières
que le produit ne peut pas franchir en test : le **réseau Vinted**, dont la réponse
catalogue est servie par un client simulé (la session du scraper est patchée, sans
Redis ni Cloudflare), et l'**envoi Telegram**, remplacé par `FakeTelegramBot`. Tout le
reste est réel, base comprise : l'alerte est une vraie ligne que le matching lit, la
charge vient du parseur et du convertisseur de production, et la légende est
construite par le code de production. Le seul arbitrage est `AI_ENABLED` coupé, ce
qui retire l'enrichissement **postérieur** à l'envoi : la légende observée est bien
celle qui part.

Trois parcours, même chemin. Les deux premiers ne diffèrent que par un champ de
l'alerte, ses `variants` : la variante valide l'annonce, sans elle rien ne part. Le
troisième est une BD dont l'alerte porte l'identité attendue : le livre déjà coté
fournit la ligne de cote. L'ISBN déclaré par le vendeur n'est pas cette confirmation.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.core.config import settings
from app.core.constants import AlertStatus, ComparisonStatus, MatchConfidence
from app.db.models.alert import Alert
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.user import User
from app.main import app
from app.services.vinted_scraper import fetch_vinted_page, to_ingest_payload
from tests.add_fakes import FakeTelegramBot
from tests.database import sqlite_only_database
from tests.screens import published_caption

TELEGRAM_CHAT_ID = 987654321

# La page de catalogue telle que Vinted la sert en JSON (API interne
# /svc-catalogue/items) : deux annonces réalistes, l'une de technologie, l'autre de
# BD. Le parseur de production en extrait les `ScrapedVintedItem`, et le convertisseur
# en déduit la charge — prix, protection (0,70 € + 5 %), port (2,88 €) et couverture —
# calculés par le produit, pas par le test.
CATALOG_FEED: dict = {
    "items": [
        {
            "id": 5512345678,
            "title": "GeForce RTX 4070 Founders Edition 12 Go",
            "price": {"amount": "399.00"},
            "item_box": {"second_line": "Très bon état"},
            "url": "/items/5512345678-geforce-rtx-4070",
            "photo": {"url": "https://images.vinted.fr/cover-rtx4070.jpg"},
        },
        {
            "id": 7712345678,
            "title": "Blacksad — Intégrale",
            "price": {"amount": "9.00"},
            "item_box": {"second_line": "Très bon état"},
            "url": "/items/7712345678-blacksad-integrale",
            "photo": {"url": "https://images.vinted.fr/cover-blacksad.jpg"},
        },
    ]
}

CATALOG_URL = "https://www.vinted.fr/catalog?catalog[]=5425"
GPU_ITEM_ID = "5512345678"
BD_ITEM_ID = "7712345678"
BD_ISBN = "9782205078054"

# Le mot-clé de l'alerte tech ne figure nulle part dans l'annonce : c'est la variante
# qui la valide. Les légendes sont les textes littéraux remis à Telegram, ligne pour
# ligne.
INCLUDE_KEYWORDS = ["nvidia rtx 4070 dual"]
VARIANT_THAT_MATCHES = "geforce rtx 4070"

EXPECTED_CAPTION = "\n".join(
    [
        "🆕 <b>GeForce RTX 4070 Founders Edition 12 Go</b>",
        "<b>Prix :</b> 399,00 € (422,53 € TTC)",
        "<b>État :</b> TBE",
        "",
        "<b>Mots-clés :</b> nvidia rtx 4070 dual",
    ]
)

# BD : la ligne de cote vient du cache déjà alimenté (prix bas constaté sur un an,
# ADR-016), comparée au total Vinted calculé par le produit.
EXPECTED_CAPTION_BD = "\n".join(
    [
        "🆕 <b>Blacksad — Intégrale</b>",
        "<b>Prix :</b> 9,00 € (13,03 € TTC)",
        "<b>État :</b> TBE",
        "",
        "<b>Prix bas constaté :</b> 18,00 €",
        "<b>Écart :</b> -4,97 € ✅",
        "",
        "<b>Mots-clés :</b> blacksad",
    ]
)


@pytest.fixture
async def ingestion_db():
    """Session SQLite en mémoire : l'alerte est une vraie ligne, lue par le matching."""
    async with sqlite_only_database() as database:
        yield database.session


async def _scraped_payload(
    item_id: str,
    *,
    category_slug: str,
    isbn13: str | None = None,
) -> dict:
    """Le chemin du producteur : fetch simulé, parse réel, charge d'ingestion.

    Le client HTTP du scraper est simulé au niveau de la requête : `fetch_vinted_page`
    tourne pour de vrai (session patchée, réponse 200 JSON servie), le parseur de
    catalogue en extrait l'annonce, et `to_ingest_payload` en fabrique la charge
    postée à l'endpoint. C'est la marche exacte de `scripts/local_vinted_poller.py`.
    """
    fake_client = AsyncMock()
    fake_client.cookies = httpx.Cookies()
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json = lambda: CATALOG_FEED
    mock_resp.text = '{"items": []}'
    fake_client.get.return_value = mock_resp

    with patch(
        "app.services.vinted_scraper.vinted_session_manager.ensure_valid_session",
        new_callable=AsyncMock,
    ):
        items = await fetch_vinted_page(CATALOG_URL, fake_client)
    item = next(scraped for scraped in items if scraped.external_id == item_id)
    return to_ingest_payload(item, category_slug=category_slug, isbn13=isbn13)


async def _seed_alert(
    session: AsyncSession,
    *,
    category: Category,
    alert_name: str,
    include_keywords: list[str],
    variants: list[str],
    max_item_price_cents: int,
) -> Alert:
    """Écrit l'alerte en base, comme le ferait `/add`, et la rend."""
    user = User(telegram_user_id=TELEGRAM_CHAT_ID, is_allowed=True)
    session.add_all([category, user])
    await session.flush()

    alert = Alert(
        user_id=user.id,
        category_id=category.id,
        name=alert_name,
        include_keywords=include_keywords,
        variants=variants,
        exclude_keywords=[],
        max_item_price_cents=max_item_price_cents,
        accepted_conditions=["Très bon état"],
        status=AlertStatus.ACTIVE,
    )
    session.add(alert)
    await session.commit()
    return alert


async def _seed_priced_book(session: AsyncSession) -> None:
    """Le livre existe déjà au catalogue, avec une cote utilisable : « déjà coté ».

    Aucune liaison alerte-livre n'est posée : c'est l'ISBN porté par l'annonce qui
    doit retrouver ce livre au moment de l'ingestion.
    """
    now = datetime.now(UTC)
    book = Book(
        isbn13=BD_ISBN,
        title="Blacksad — Intégrale",
        normalized_title="blacksad integrale",
    )
    book.market_cache = BookMarketCache(
        lowest_delivered_price_cents=2000,
        historical_low_1y_cents=1800,
        best_provider_name="momox",
        best_source_url="https://momox-shop.fr/blacksad",
        confidence=MatchConfidence.HIGH,
        checked_at=now,
        expires_at=now + timedelta(days=1),
        comparison_status=ComparisonStatus.AVAILABLE,
    )
    session.add(book)
    await session.commit()


async def _post_listing(session: AsyncSession, bot: FakeTelegramBot, payload: dict):
    """Poste la charge sur l'endpoint, le bot observant ce qui part réellement."""

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        # Le bot est la frontière Telegram : le remplacer est la seule couture, et
        # `send_listing_alert` comme sa légende restent ceux de production.
        with patch("app.use_cases.ingest_listing.default_bot", bot):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={
                    "Authorization": (
                        "Bearer " + settings.INGEST_API_TOKEN.get_secret_value()
                    )
                },
            ) as client:
                return await client.post("/api/v1/ingest/listings", json=payload)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_a_matching_listing_reaches_the_telegram_message(
    ingestion_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La variante valide l'annonce scrapée, et la légende attendue part à Telegram."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    await _seed_alert(
        ingestion_db,
        category=Category(slug="tech", label="Tech", emoji="💻", is_active=True),
        alert_name="NVIDIA RTX 4070",
        include_keywords=INCLUDE_KEYWORDS,
        variants=[VARIANT_THAT_MATCHES],
        max_item_price_cents=45000,
    )
    bot = FakeTelegramBot()

    payload = await _scraped_payload(GPU_ITEM_ID, category_slug="tech")
    response = await _post_listing(ingestion_db, bot, payload)

    assert response.status_code == 200
    body = response.json()
    assert body["is_duplicate"] is False
    assert [(item["alert_name"], item["notification_sent"]) for item in body["matches"]] == [
        ("NVIDIA RTX 4070", True)
    ]

    assert len(bot.sent_to_telegram) == 1
    delivered = bot.sent_to_telegram[0]
    assert delivered["chat_id"] == TELEGRAM_CHAT_ID
    assert published_caption(bot) == EXPECTED_CAPTION
    # L'annonce a une couverture : le message part en légende de photo, pas en texte.
    bot.send_photo.assert_awaited_once()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_listing_that_matches_no_alert_sends_nothing(
    ingestion_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La même annonce scrapée, la même alerte privée de sa variante : rien ne part.

    Le mot-clé de l'alerte est absent de l'annonce ; sans la variante, le matching ne
    peut plus la valider. C'est la moitié négative de la preuve : elle montre que le
    message du premier parcours n'était pas dû au hasard, mais bien à cette variante.
    """
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    await _seed_alert(
        ingestion_db,
        category=Category(slug="tech", label="Tech", emoji="💻", is_active=True),
        alert_name="NVIDIA RTX 4070",
        include_keywords=INCLUDE_KEYWORDS,
        variants=[],
        max_item_price_cents=45000,
    )
    bot = FakeTelegramBot()

    payload = await _scraped_payload(GPU_ITEM_ID, category_slug="tech")
    response = await _post_listing(ingestion_db, bot, payload)

    assert response.status_code == 200
    assert response.json()["matches"] == []
    assert bot.sent_to_telegram == []
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_isbn_alert_delivers_the_priced_book_cote_line(
    ingestion_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L'identité portée par l'alerte retrouve la cote locale avant l'envoi réel."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    alert = await _seed_alert(
        ingestion_db,
        category=Category(
            slug="bd_comics", label="BD & Comics", emoji="📚", is_active=True
        ),
        alert_name="Blacksad",
        include_keywords=["blacksad"],
        variants=[],
        max_item_price_cents=1500,
    )
    alert.isbn = BD_ISBN
    await ingestion_db.commit()
    await _seed_priced_book(ingestion_db)
    bot = FakeTelegramBot()

    payload = await _scraped_payload(
        BD_ITEM_ID, category_slug="bd_comics", isbn13=BD_ISBN
    )
    response = await _post_listing(ingestion_db, bot, payload)

    assert response.status_code == 200
    body = response.json()
    assert body["is_duplicate"] is False
    assert [(item["alert_name"], item["notification_sent"]) for item in body["matches"]] == [
        ("Blacksad", True)
    ]

    assert len(bot.sent_to_telegram) == 1
    delivered = bot.sent_to_telegram[0]
    assert delivered["chat_id"] == TELEGRAM_CHAT_ID
    assert published_caption(bot) == EXPECTED_CAPTION_BD
    bot.send_photo.assert_awaited_once()
    bot.send_message.assert_not_awaited()
