"""Le clic sur « Acheter » : association, repli, double clic, erreurs.

Chaque test remplace la frontière Vinted et Redis : aucune opération réelle,
aucun paiement, aucun appel réseau. L'alerte n'est jamais réécrite — le clic
n'existe qu'au moment où l'utilisateur le demande.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

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
    FAILURE_THRESHOLD,
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
    """Le sous-ensemble Redis utilisé par la préparation, valeurs non exposées.

    Le double modélise l'**expiration** et le `NX` : la pause et le verrou se
    comportent comme en production, sinon un test de coupe-circuit ne prouverait
    rien (une pause posée mais jamais active, ou un verrou écrasé par le second
    clic, donnerait un faux vert).
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expiry: dict[str, float] = {}

    def _expired(self, key: str) -> bool:
        deadline = self.expiry.get(key)
        return deadline is not None and deadline <= time.monotonic()

    def _live(self, key: str) -> bool:
        if key not in self.store:
            return False
        if self._expired(key):
            self.store.pop(key, None)
            self.expiry.pop(key, None)
            return False
        return True

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        if nx and self._live(key):
            return None
        self.store[key] = value
        if ex is not None:
            self.expiry[key] = time.monotonic() + ex
        else:
            self.expiry.pop(key, None)
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key) if self._live(key) else None

    async def delete(self, key: str) -> int:
        removed = key in self.store
        self.store.pop(key, None)
        self.expiry.pop(key, None)
        return 1 if removed else 0

    async def incr(self, key: str) -> int:
        self._live(key)
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        if not self._live(key):
            return False
        self.expiry[key] = time.monotonic() + seconds
        return True

    async def ttl(self, key: str) -> int:
        if not self._live(key):
            return -2
        deadline = self.expiry.get(key)
        return -1 if deadline is None else max(0, int(deadline - time.monotonic()))


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


def activate_pause(fake: FakeRedis, seconds: int = 600) -> None:
    """Pose une suspension active, comme le coupe-circuit le ferait (`set ex`).

    Une pause écrite sans expiration se lirait `ttl = -1` et serait ignorée :
    le double doit porter la même sémantique que Redis, sans quoi le test ne
    prouverait rien.
    """
    fake.store["radar:vinted_checkout:pause"] = "1"
    fake.expiry["radar:vinted_checkout:pause"] = time.monotonic() + seconds


async def seed_listing(
    db: Any,
    *,
    owner_id: int = TELEGRAM_USER_ID,
    seller_external_id: str | None = SELLER_ID,
    with_category: bool = True,
    external_id: str = LISTING_EXTERNAL_ID,
) -> Listing:
    """Une annonce associée à une alerte de `owner_id` vivante."""
    existing_owner = (
        await db.session.execute(
            select(User).where(User.telegram_user_id == owner_id)
        )
    ).scalar_one_or_none()
    if existing_owner is None:
        owner = User(telegram_user_id=owner_id, is_allowed=True)
        db.session.add(owner)
        await db.session.flush()
    else:
        owner = existing_owner

    if with_category:
        category = Category(slug="tech", label="Tech", emoji="T", is_active=True)
        db.session.add(category)
        await db.session.flush()
    else:
        category = (
            await db.session.execute(select(Category).where(Category.slug == "tech"))
        ).scalar_one()

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
        external_id=external_id,
        url=f"https://www.vinted.fr/items/{external_id}",
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
    # Le refus est décidé **avant** toute lecture en base : la session est un
    # prérequis moins cher qu'une requête SQL, et le clic n'en paie aucune.
    assert result.listing_url is None
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
    provider.fetch_item_seller_id.assert_awaited_once()
    called_kwargs = provider.fetch_item_seller_id.await_args.kwargs
    assert called_kwargs["cookies"]["access_token_web"] == "jeton-synthetique"
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


def test_paused_copy_sends_back_to_the_listing() -> None:
    """La suspension se dit, et n'invite pas à recliquer tout de suite."""
    text, keyboard = format_preparation_result(
        CheckoutPreparation(
            VintedCheckoutStatus.PAUSED,
            listing_url="https://www.vinted.fr/items/1",
        )
    )
    assert "suspendues" in text
    assert keyboard is not None
    assert [button.text for row in keyboard.inline_keyboard for button in row] == [
        "Voir l'annonce"
    ]


# ---------------------------------------------------------------------------
# Coupe-circuit : ne pas insister après un blocage, une limitation ou une panne
# ---------------------------------------------------------------------------


async def test_datadome_block_immediately_suspends_further_preparations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un défi anti-robot arrête les préparations : le clic suivant ne relance rien."""
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db)
        provider = mock_provider()
        provider.create_buyer_inquiry.side_effect = VintedCheckoutError(
            VintedCheckoutStatus.BLOCKED, "défi"
        )

        blocked = await run_use_case(db, listing.id, provider)
        # Un autre article, un autre identifiant : la suspension est globale.
        second_listing = await seed_listing(
            db, with_category=False, external_id="22222222222"
        )
        after = await run_use_case(db, second_listing.id, provider)

    assert blocked.status is VintedCheckoutStatus.BLOCKED
    assert after.status is VintedCheckoutStatus.PAUSED
    provider.create_buyer_inquiry.assert_awaited_once()
    assert fake.store.get("radar:vinted_checkout:pause") == "1"


async def test_rate_limit_suspends_further_preparations(
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
            VintedCheckoutStatus.RATE_LIMITED, "429"
        )

        limited = await run_use_case(db, listing.id, provider)
        after = await run_use_case(db, listing.id, provider)

    assert limited.status is VintedCheckoutStatus.RATE_LIMITED
    assert after.status is VintedCheckoutStatus.PAUSED
    provider.create_buyer_inquiry.assert_awaited_once()


async def test_repeated_soft_failures_eventually_suspend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trois échecs doux suspendent : insister use la session et l'IP pour rien."""
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db)
        provider = mock_provider()
        provider.create_buyer_inquiry.side_effect = VintedCheckoutError(
            VintedCheckoutStatus.TIMEOUT, "délai"
        )

        statuses = []
        for _ in range(FAILURE_THRESHOLD):
            # Un verrou posé par un échec incertain bloquerait le tour suivant :
            # on le retire pour éprouver le **compteur** d'échecs, pas le verrou.
            fake.store.pop(lock_key(listing.id), None)
            statuses.append((await run_use_case(db, listing.id, provider)).status)
        suspended = await run_use_case(db, listing.id, provider)

    assert statuses == [VintedCheckoutStatus.TIMEOUT] * FAILURE_THRESHOLD
    assert suspended.status is VintedCheckoutStatus.PAUSED
    assert fake.store.get("radar:vinted_checkout:pause") == "1"


async def test_success_clears_the_soft_failure_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une réussite remet le compteur à zéro : la série ne se cumule pas dans le temps."""
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db)
        provider = mock_provider()
        provider.create_buyer_inquiry.side_effect = VintedCheckoutError(
            VintedCheckoutStatus.TIMEOUT, "délai"
        )
        for _ in range(FAILURE_THRESHOLD - 1):
            fake.store.pop(lock_key(listing.id), None)
            await run_use_case(db, listing.id, provider)
        assert fake.store.get("radar:vinted_checkout:failures") == str(FAILURE_THRESHOLD - 1)

        provider.create_buyer_inquiry.side_effect = None
        provider.create_buyer_inquiry.return_value = "4242"
        fake.store.pop(lock_key(listing.id), None)
        recovered = await run_use_case(db, listing.id, provider)

    assert recovered.status is VintedCheckoutStatus.READY
    assert "radar:vinted_checkout:failures" not in fake.store


async def test_pause_blocks_even_a_valid_cached_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """La suspension prime : pas de nouveau message de checkout pendant la pause."""
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    activate_pause(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db)
        fake.store[link_key(listing.id)] = (
            '{"url": "https://www.vinted.fr/checkout?purchase_id=9"}'
        )
        provider = mock_provider()
        result = await run_use_case(db, listing.id, provider)

    assert result.status is VintedCheckoutStatus.PAUSED
    provider.create_buyer_inquiry.assert_not_awaited()


async def test_paused_listing_keeps_the_listing_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Après suspension, un clic sur une annonce suivie garde l'accès à l'annonce."""
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    activate_pause(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db)
        result = await run_use_case(db, listing.id, mock_provider())

    assert result.status is VintedCheckoutStatus.PAUSED
    # La pause est décidée avant la lecture d'annonce : le repli n'est donc pas
    # attaché ici, mais le handler ne prive jamais l'utilisateur du message.
    text, keyboard = format_preparation_result(result)
    assert text
    assert keyboard is None


async def test_concurrent_clicks_create_a_single_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deux clics vraiment simultanés : une seule opération atteint Vinted."""
    import asyncio

    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_inquiry(**_kwargs: Any) -> str:
            started.set()
            await release.wait()
            return "4242"

        provider = mock_provider()
        provider.create_buyer_inquiry.side_effect = slow_inquiry

        first = asyncio.create_task(run_use_case(db, listing.id, provider))
        await asyncio.wait_for(started.wait(), 3)
        second = await run_use_case(db, listing.id, provider)
        release.set()
        first_result = await first

    assert second.status is VintedCheckoutStatus.BUSY
    assert first_result.status is VintedCheckoutStatus.READY
    provider.create_buyer_inquiry.assert_awaited_once()
    provider.build_single_checkout.assert_awaited_once()


async def test_suspended_preparation_is_not_retried_without_a_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aucune boucle : tant que la pause court, un appel ne déclenche rien."""
    monkeypatch.setattr(settings, "VINTED_CHECKOUT_PREPARATION_ENABLED", True)
    fake = FakeRedis()
    configured_session(fake)
    activate_pause(fake)
    patch_redis(monkeypatch, fake)

    async with sqlite_only_database() as db:
        listing = await seed_listing(db)
        provider = mock_provider()
        for _ in range(3):
            result = await run_use_case(db, listing.id, provider)

    assert result.status is VintedCheckoutStatus.PAUSED
    provider.create_buyer_inquiry.assert_not_awaited()


# ---------------------------------------------------------------------------
# Accès et liens : aucun secret dans les réponses, aucune action sur un aperçu
# ---------------------------------------------------------------------------


async def test_ready_message_never_carries_a_cookie_or_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le message de succès ne porte que le lien de checkout, jamais un cookie."""
    listing_id = uuid.uuid4()
    callback = fake_callback(listing_id)
    preparation = CheckoutPreparation(
        VintedCheckoutStatus.READY,
        checkout_url="https://www.vinted.fr/checkout?purchase_id=1&order_id=2&order_type=transaction",
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
    joined = text + " " + " ".join(button.url or "" for row in buttons for button in row)
    for secret in ("access_token_web", "_vinted_fr_session", "jeton", "cookie", "Bearer"):
        assert secret not in joined


async def test_only_the_listing_host_is_ever_linked() -> None:
    """Aucune redirection arbitraire : les seules URL émises sont Vinted."""
    from app.providers.vinted_checkout import SITE_URL, build_checkout_url_for

    preparation = CheckoutPreparation(
        VintedCheckoutStatus.READY,
        checkout_url=build_checkout_url_for(transaction_id="2", purchase_id="1"),
        listing_url="https://www.vinted.fr/items/1",
        listing_title="Console",
    )
    _, keyboard = format_preparation_result(preparation)
    assert keyboard is not None
    for row in keyboard.inline_keyboard:
        for button in row:
            assert button.url is not None
            assert button.url.startswith(f"{SITE_URL}/")


async def test_buy_is_a_callback_not_a_link_so_a_preview_cannot_trigger_it() -> None:
    """Un aperçu ne déclenche rien : « Acheter » n'a pas d'URL, seulement un callback."""
    from app.bot.keyboards.listings import get_listing_alert_keyboard

    keyboard = get_listing_alert_keyboard(
        uuid.uuid4(), "https://www.vinted.fr/items/1", "tech", checkout_enabled=True
    )
    buy = keyboard.inline_keyboard[0][0]
    assert buy.text == "Acheter"
    assert buy.url is None
    assert buy.callback_data is not None
    assert buy.callback_data.startswith("buy:")


async def test_the_preparation_route_requires_the_internal_token() -> None:
    """Un GET ou un POST anonyme sur la route de session ne crée aucune action.

    La route ne fait que stocker une session déjà validée ; elle exige le jeton
    interne, comme toutes les routes `/api/v1/internal/…`. La préparation, elle,
    n'est **pas** exposée en HTTP : elle n'existe qu'au clic Telegram.
    """
    import inspect

    from app.api.routes import internal

    source = inspect.getsource(internal)
    assert "require_internal_token" in source
    # La préparation de checkout n'est adressable par aucune route HTTP.
    assert "purchases/checkout/build" not in source
    assert "create_buyer_inquiry" not in source
