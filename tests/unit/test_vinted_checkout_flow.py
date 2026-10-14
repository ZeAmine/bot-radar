"""Le clic sur « Acheter » : association, repli, double clic, erreurs.

Chaque test remplace la frontière Vinted et Redis : aucune opération réelle,
aucun paiement, aucun appel réseau. L'alerte n'est jamais réécrite — le clic
n'existe qu'au moment où l'utilisateur le demande.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.vinted_checkout import (
    format_preparation_result,
    handle_buy_listing,
)
from app.bot.middlewares.auth import AuthMiddleware
from app.core.config import settings
from app.db.models.alert import Alert
from app.db.models.category import Category
from app.db.models.listing import Listing
from app.db.models.listing_match import ListingMatch
from app.db.models.user import User
from app.domain.enums import AlertStatus, VintedCheckoutStatus
from app.providers.vinted_checkout import (
    REDIS_CHECKOUT_SESSION_KEY,
    VintedCheckoutError,
    VintedCheckoutProvider,
)
from app.use_cases.prepare_vinted_checkout import (
    LOCK_TTL_SECONDS,
    CheckoutPreparation,
    PrepareVintedCheckoutUseCase,
)
from tests.database import sqlite_only_database

TELEGRAM_USER_ID = 601
OTHER_USER_ID = 999
LISTING_EXTERNAL_ID = "10373403907"
SELLER_ID = "72804845"


class FakeRedis:
    """Le sous-ensemble Redis utilisé par la préparation, valeurs non exposées."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        del ex  # le double n'a pas d'expiration : seule la présence compte
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


def link_key(listing_id: uuid.UUID, telegram_user_id: int = TELEGRAM_USER_ID) -> str:
    return f"radar:vinted_checkout:link:{telegram_user_id}:{listing_id}"


def lock_key(listing_id: uuid.UUID, telegram_user_id: int = TELEGRAM_USER_ID) -> str:
    return f"radar:vinted_checkout:lock:{telegram_user_id}:{listing_id}"


def patch_redis(monkeypatch: pytest.MonkeyPatch, fake: FakeRedis) -> None:
    getter = lambda: fake  # noqa: E731 - double de test, pas une règle métier
    monkeypatch.setattr("app.use_cases.prepare_vinted_checkout.get_redis_client", getter)
    monkeypatch.setattr("app.providers.vinted_checkout.get_redis_client", getter)


def configured_session(fake: FakeRedis) -> None:
    fake.store[REDIS_CHECKOUT_SESSION_KEY] = (
        '{"access_token_web": "jeton-synthetique", "_vinted_fr_session": "session"}'
    )


async def seed_listing(
    db: Any,
    *,
    owner_id: int = TELEGRAM_USER_ID,
    seller_external_id: str | None = SELLER_ID,
) -> Listing:
    """Une annonce associée à une alerte de `owner_id` vivante."""
    category = Category(slug="tech", label="Tech", emoji="T", is_active=True)
    owner = User(telegram_user_id=owner_id, is_allowed=True)
    db.session.add_all([category, owner])
    await db.session.flush()
    alert = Alert(
        user_id=owner.id,
        category_id=category.id,
        name="Console",
        include_keywords=["console"],
        status=AlertStatus.ACTIVE,
    )
    db.session.add(alert)
    await db.session.flush()
    raw: dict[str, Any] = {}
    if seller_external_id:
        raw["seller_external_id"] = seller_external_id
    listing = Listing(
        source_name="vinted",
        external_id=LISTING_EXTERNAL_ID,
        url=f"https://www.vinted.fr/items/{LISTING_EXTERNAL_ID}",
        title="Console",
        normalized_title="console",
        category_slug="tech",
        item_price_cents=1000,
        raw_payload=raw,
    )
    db.session.add(listing)
    await db.session.flush()
    db.session.add(
        ListingMatch(
            alert_id=alert.id,
            listing_id=listing.id,
            notified_at=datetime.now(UTC),
            notification_state="notified",
        )
    )
    await db.session.commit()
    return listing


def mock_provider() -> AsyncMock:
    provider = AsyncMock(spec=VintedCheckoutProvider)
    provider.create_buyer_inquiry.return_value = "4242"
    provider.build_single_checkout.return_value = "98765"
    return provider


async def run_use_case(
    db: Any,
    listing_id: uuid.UUID,
    provider: AsyncMock,
    *,
    telegram_user_id: int = TELEGRAM_USER_ID,
) -> CheckoutPreparation:
    return await PrepareVintedCheckoutUseCase(db.session, provider=provider).execute(
        telegram_user_id=telegram_user_id, listing_id=listing_id
    )


# ---------------------------------------------------------------------------
# Cas d'usage
# ---------------------------------------------------------------------------


async def test_flag_off_is_announced_without_preparing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", False)
    fake = FakeRedis()
    configured_session(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        provider = mock_provider()
        result = await run_use_case(db, uuid.uuid4(), provider)

    assert result.status is VintedCheckoutStatus.DISABLED
    assert result.checkout_url is None
    provider.create_buyer_inquiry.assert_not_awaited()


async def test_listing_without_alert_of_this_user_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db, owner_id=OTHER_USER_ID)
        provider = mock_provider()
        result = await run_use_case(db, listing.id, provider)

    assert result.status is VintedCheckoutStatus.NOT_ASSOCIATED
    provider.create_buyer_inquiry.assert_not_awaited()


async def test_missing_session_is_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db)
        provider = mock_provider()
        result = await run_use_case(db, listing.id, provider)

    assert result.status is VintedCheckoutStatus.NOT_CONFIGURED
    assert result.listing_url == listing.url
    provider.create_buyer_inquiry.assert_not_awaited()


async def test_ready_prepares_once_and_reuses_the_cached_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db)
        provider = mock_provider()

        first = await run_use_case(db, listing.id, provider)
        second = await run_use_case(db, listing.id, provider)

    assert first.status is VintedCheckoutStatus.READY
    assert first.checkout_url == (
        "https://www.vinted.fr/checkout?purchase_id=98765&order_id=4242&order_type=transaction"
    )
    assert second.status is VintedCheckoutStatus.ALREADY_READY
    assert second.checkout_url == first.checkout_url
    provider.create_buyer_inquiry.assert_awaited_once()
    provider.build_single_checkout.assert_awaited_once()
    assert link_key(listing.id) in fake.store


async def test_double_click_on_a_locked_listing_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db)
        fake.store[lock_key(listing.id)] = "1"
        provider = mock_provider()
        result = await run_use_case(db, listing.id, provider)

    assert result.status is VintedCheckoutStatus.BUSY
    provider.create_buyer_inquiry.assert_not_awaited()


async def test_session_refusal_releases_the_lock_for_a_later_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db)
        provider = mock_provider()
        provider.create_buyer_inquiry.side_effect = VintedCheckoutError(
            VintedCheckoutStatus.AUTH_REQUIRED, "session refusée"
        )

        refused = await run_use_case(db, listing.id, provider)
        assert refused.status is VintedCheckoutStatus.AUTH_REQUIRED
        assert lock_key(listing.id) not in fake.store

        provider.create_buyer_inquiry.side_effect = None
        provider.create_buyer_inquiry.return_value = "4242"
        retried = await run_use_case(db, listing.id, provider)

    assert retried.status is VintedCheckoutStatus.READY
    assert provider.create_buyer_inquiry.await_count == 2


async def test_uncertain_timeout_keeps_the_lock_against_a_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un délai dépassé peut cacher une opération aboutie : pas de relance immédiate."""
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db)
        provider = mock_provider()
        provider.create_buyer_inquiry.side_effect = VintedCheckoutError(
            VintedCheckoutStatus.TIMEOUT, "délai dépassé"
        )

        timed_out = await run_use_case(db, listing.id, provider)
        again = await run_use_case(db, listing.id, provider)

    assert timed_out.status is VintedCheckoutStatus.TIMEOUT
    assert LOCK_TTL_SECONDS > 0
    assert lock_key(listing.id) in fake.store
    assert again.status is VintedCheckoutStatus.BUSY
    provider.create_buyer_inquiry.assert_awaited_once()


async def test_unavailable_listing_is_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db)
        provider = mock_provider()
        provider.create_buyer_inquiry.side_effect = VintedCheckoutError(
            VintedCheckoutStatus.UNAVAILABLE, "vendu"
        )
        result = await run_use_case(db, listing.id, provider)

    assert result.status is VintedCheckoutStatus.UNAVAILABLE
    assert result.listing_url == listing.url


async def test_missing_seller_id_is_read_from_the_public_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db, seller_external_id=None)
        provider = mock_provider()
        provider.fetch_item_seller_id.return_value = SELLER_ID
        result = await run_use_case(db, listing.id, provider)

    assert result.status is VintedCheckoutStatus.READY
    provider.fetch_item_seller_id.assert_awaited_once_with(LISTING_EXTERNAL_ID)
    assert provider.create_buyer_inquiry.await_args.kwargs["seller_id"] == SELLER_ID


async def test_unidentified_seller_is_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db, seller_external_id=None)
        provider = mock_provider()
        provider.fetch_item_seller_id.return_value = None
        result = await run_use_case(db, listing.id, provider)

    assert result.status is VintedCheckoutStatus.SELLER_UNKNOWN
    provider.create_buyer_inquiry.assert_not_awaited()


# ---------------------------------------------------------------------------
# Handler Telegram : autorisation, message, repli
# ---------------------------------------------------------------------------


def fake_callback(listing_id: uuid.UUID, *, user_id: int = TELEGRAM_USER_ID) -> MagicMock:
    message = MagicMock(spec=Message)
    message.reply = AsyncMock()
    message.edit_text = AsyncMock()
    message.edit_reply_markup = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.data = f"buy:{listing_id}"
    callback.answer = AsyncMock()
    callback.message = message
    callback.from_user = MagicMock(id=user_id)
    return callback


async def test_stranger_cannot_trigger_the_buy_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L'allowlist existante protège le bouton : un étranger n'atteint jamais le handler."""
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", [TELEGRAM_USER_ID])
    callback = fake_callback(uuid.uuid4(), user_id=OTHER_USER_ID)
    handler = AsyncMock()

    result = await AuthMiddleware()(handler, callback, {"event_from_user": callback.from_user})

    assert result is None
    handler.assert_not_called()
    callback.answer.assert_awaited_once_with(AuthMiddleware.UNAUTHORIZED_MESSAGE, show_alert=True)


async def test_handler_replies_with_checkout_link_and_listing_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing_id = uuid.uuid4()
    callback = fake_callback(listing_id)
    preparation = CheckoutPreparation(
        VintedCheckoutStatus.READY,
        checkout_url="https://www.vinted.fr/checkout?purchase_id=1&order_id=2&order_type=transaction",
        listing_url="https://www.vinted.fr/items/1",
        listing_title="Console <b>pro</b>",
    )

    class FakeUseCase:
        def __init__(self, db: Any) -> None:
            pass

        async def execute(self, *, telegram_user_id: int, listing_id: uuid.UUID) -> Any:
            assert telegram_user_id == TELEGRAM_USER_ID
            assert listing_id
            return preparation

    monkeypatch.setattr(
        "app.bot.handlers.vinted_checkout.PrepareVintedCheckoutUseCase", FakeUseCase
    )
    await handle_buy_listing(callback, MagicMock())

    callback.answer.assert_awaited_once()
    text = callback.message.reply.await_args.args[0]
    buttons = callback.message.reply.await_args.kwargs["reply_markup"].inline_keyboard
    assert "Checkout Vinted prêt" in text
    assert "Console &lt;b&gt;pro&lt;/b&gt;" in text  # titre échappé, pas injecté
    assert [[button.text for button in row] for row in buttons] == [
        ["Poursuivre sur Vinted"],
        ["Voir l'annonce"],
    ]
    assert buttons[0][0].url == preparation.checkout_url
    assert buttons[1][0].url == preparation.listing_url
    # L'alerte d'origine n'est ni éditée ni supprimée : la réponse est un message neuf.
    callback.message.edit_text.assert_not_awaited()
    callback.message.edit_reply_markup.assert_not_awaited()


async def test_handler_keeps_listing_access_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing_id = uuid.uuid4()
    callback = fake_callback(listing_id)
    preparation = CheckoutPreparation(
        VintedCheckoutStatus.BLOCKED,
        listing_url="https://www.vinted.fr/items/1",
        listing_title="Console",
    )

    class FakeUseCase:
        def __init__(self, db: Any) -> None:
            pass

        async def execute(self, *, telegram_user_id: int, listing_id: uuid.UUID) -> Any:
            assert telegram_user_id == TELEGRAM_USER_ID
            assert listing_id
            return preparation

    monkeypatch.setattr(
        "app.bot.handlers.vinted_checkout.PrepareVintedCheckoutUseCase", FakeUseCase
    )
    await handle_buy_listing(callback, MagicMock())

    text = callback.message.reply.await_args.args[0]
    buttons = callback.message.reply.await_args.kwargs["reply_markup"].inline_keyboard
    assert "vérification anti-robot" in text
    assert [[button.text for button in row] for row in buttons] == [["Voir l'annonce"]]


def test_refusal_without_a_known_listing_has_no_dead_button() -> None:
    text, keyboard = format_preparation_result(
        CheckoutPreparation(VintedCheckoutStatus.NOT_ASSOCIATED)
    )
    assert "pas associée à tes alertes" in text
    assert keyboard is None


def test_ready_copy_never_promises_an_automatic_payment() -> None:
    text, _ = format_preparation_result(
        CheckoutPreparation(
            VintedCheckoutStatus.READY,
            checkout_url="https://www.vinted.fr/checkout?purchase_id=1",
            listing_url="https://www.vinted.fr/items/1",
            listing_title="Console",
        )
    )
    assert "paiement reste entièrement manuel" in text
    assert "point relais" in text
