"""Parcours d'ingestion : de l'annonce reçue au message remis à Telegram.

Le chemin réel est traversé de bout en bout, sans le harnais `/add` :
`POST /api/v1/ingest/listings` → `ProcessListingUseCase` →
`evaluate_listing_against_alert` (la couture qui porte les mots-clés et les variantes)
→ `send_listing_alert` → `build_listing_caption` → `Bot.send_photo`/`Bot.send_message`.

Ce que le test faux, ce sont les deux frontières que le produit ne peut pas franchir en
test : la **source d'annonces**, dont la charge réaliste est postée à la place du
poller, et l'**envoi Telegram**, remplacé par `FakeTelegramBot`. Tout le reste est réel,
base comprise : l'alerte est une ligne SQLite que le matching lit vraiment, et la
légende est construite par le code de production. Le seul arbitrage est `AI_ENABLED`
coupé, ce qui retire l'enrichissement **postérieur** à l'envoi : la légende observée
est bien celle qui part.

Les deux parcours ne diffèrent que par un champ de l'alerte, ses `variants`. Le positif
n'aboutit que si la variante valide l'annonce ; le négatif montre qu'à variante absente,
rien ne part.
"""

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.core.config import settings
from app.core.constants import AlertStatus
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.user import User
from app.main import app
from tests.add_fakes import FakeTelegramBot
from tests.database import fresh_database
from tests.screens import published_caption

TELEGRAM_CHAT_ID = 987654321

# L'annonce telle que la source la livre : une carte graphique Vinted, avec sa photo de
# couverture, son port et sa protection. Le total (405,98 €) est la somme article +
# protection + port, calculée par le produit, pas par le test.
LISTING = {
    "source_name": "vinted",
    "external_id": "vinted-gpu-4070-0001",
    "url": "https://www.vinted.fr/items/5512345678",
    "title": "GeForce RTX 4070 Founders Edition 12 Go",
    "description": (
        "Carte graphique achetée en 2024, jamais overclockée, boîte et facture fournies."
    ),
    "category_slug": "tech",
    "item_price_cents": 39900,
    "estimated_protection_cents": 199,
    "estimated_shipping_cents": 499,
    "condition": "Très bon état",
    "seller_name": "vendeur-gpu",
    "seller_rating": 4.8,
    "photos": ["https://images.vinted.fr/cover-rtx4070.jpg"],
}

# Le mot-clé de l'alerte ne figure nulle part dans l'annonce : c'est la variante qui la
# valide. La légende est le texte littéral remis à Telegram, ligne pour ligne.
INCLUDE_KEYWORDS = ["nvidia rtx 4070 dual"]
VARIANT_THAT_MATCHES = "geforce rtx 4070"

EXPECTED_CAPTION = "\n".join(
    [
        "🆕 <b>GeForce RTX 4070 Founders Edition 12 Go</b>",
        "<b>Prix :</b> 399,00 € (405,98 € TTC)",
        "<b>État :</b> TBE",
        "",
        "<b>Mots-clés :</b> nvidia rtx 4070 dual",
    ]
)


@pytest.fixture
async def ingestion_db():
    """Session SQLite en mémoire : l'alerte est une vraie ligne, lue par le matching."""
    async with fresh_database(replayed_on_postgres=False) as (session, _maker):
        yield session


async def _seed_alert(
    session: AsyncSession,
    *,
    include_keywords: list[str],
    variants: list[str],
) -> Alert:
    """Écrit l'alerte en base, comme le ferait `/add`, et la rend."""
    category = Category(slug="tech", label="Tech", emoji="💻", is_active=True)
    user = User(telegram_user_id=TELEGRAM_CHAT_ID, is_allowed=True)
    session.add_all([category, user])
    await session.flush()

    alert = Alert(
        user_id=user.id,
        category_id=category.id,
        name="NVIDIA RTX 4070",
        include_keywords=include_keywords,
        variants=variants,
        exclude_keywords=[],
        max_item_price_cents=45000,
        accepted_conditions=["Très bon état"],
        status=AlertStatus.ACTIVE,
    )
    session.add(alert)
    await session.commit()
    return alert


async def _post_listing(session: AsyncSession, bot: FakeTelegramBot):
    """Poste l'annonce sur l'endpoint, le bot observant ce qui part réellement."""

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
                return await client.post("/api/v1/ingest/listings", json=LISTING)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_a_matching_listing_reaches_the_telegram_message(
    ingestion_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La variante valide l'annonce, et la légende attendue part à Telegram."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    await _seed_alert(
        ingestion_db,
        include_keywords=INCLUDE_KEYWORDS,
        variants=[VARIANT_THAT_MATCHES],
    )
    bot = FakeTelegramBot()

    response = await _post_listing(ingestion_db, bot)

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
    """La même annonce, la même alerte privée de sa variante : rien ne part.

    Le mot-clé de l'alerte est absent de l'annonce ; sans la variante, le matching ne
    peut plus la valider. C'est la moitié négative de la preuve : elle montre que le
    message du premier parcours n'était pas dû au hasard, mais bien à cette variante.
    """
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    await _seed_alert(
        ingestion_db,
        include_keywords=INCLUDE_KEYWORDS,
        variants=[],
    )
    bot = FakeTelegramBot()

    response = await _post_listing(ingestion_db, bot)

    assert response.status_code == 200
    assert response.json()["matches"] == []
    assert bot.sent_to_telegram == []
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_not_awaited()
