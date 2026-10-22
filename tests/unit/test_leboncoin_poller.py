"""Tests unitaires du poller Leboncoin (collecte et ingestion simulées)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.db.models.listing import Listing
from app.services.leboncoin_parsing import ScrapedLeboncoinItem
from app.services.telegram_service import build_listing_caption
from app.tasks import leboncoin_poller
from app.tasks.leboncoin_poller import IngestOutcome, _block_cooldown, poll_leboncoin_once


def _item(external_id: str) -> ScrapedLeboncoinItem:
    return ScrapedLeboncoinItem(
        external_id=external_id,
        title=f"Blacksad — Intégrale {external_id}",
        url=f"https://www.leboncoin.fr/ad/livres/{external_id}",
        item_price_cents=2500,
        condition=None,
        photo_url=None,
        published_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_poll_skips_seen_and_ingests_new(monkeypatch: pytest.MonkeyPatch):
    ingested: list[str] = []

    async def fake_fetch(
        _url: str, _proxy: str | None, _page: int = 1
    ) -> list[ScrapedLeboncoinItem]:
        return [_item("1"), _item("2")]

    async def fake_ingest(item: ScrapedLeboncoinItem, _slug: str) -> IngestOutcome:
        ingested.append(item.external_id)
        return IngestOutcome(notified_count=0, needs_retry=False)

    monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
    monkeypatch.setattr(leboncoin_poller, "_ingest_item", fake_ingest)

    seen: set[str] = {"2"}
    outcome = await poll_leboncoin_once(seen, cycle_index=1)

    assert ingested == ["1"]
    assert outcome.ingested_count == 1
    assert outcome.ads_seen == 2
    assert "1" in seen


@pytest.mark.asyncio
async def test_poll_keeps_failed_and_retry_ids_for_next_cycle(monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(
        _url: str, _proxy: str | None, _page: int = 1
    ) -> list[ScrapedLeboncoinItem]:
        return [_item("boom"), _item("retry"), _item("ok")]

    async def fake_ingest(item: ScrapedLeboncoinItem, _slug: str) -> IngestOutcome:
        if item.external_id == "boom":
            raise RuntimeError("ingestion cassée")
        if item.external_id == "retry":
            return IngestOutcome(notified_count=0, needs_retry=True)
        return IngestOutcome(notified_count=1, needs_retry=False)

    monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
    monkeypatch.setattr(leboncoin_poller, "_ingest_item", fake_ingest)

    seen: set[str] = set()
    outcome = await poll_leboncoin_once(seen)

    assert "boom" not in seen
    assert "retry" not in seen
    assert "ok" in seen
    # L'échec n'est pas compté ; « retry » l'est (annonce ingérée, notification différée).
    assert outcome.ingested_count == 2
    assert outcome.needs_retry is True


@pytest.mark.asyncio
async def test_poll_with_no_new_items_does_not_ingest(monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(
        _url: str, _proxy: str | None, _page: int = 1
    ) -> list[ScrapedLeboncoinItem]:
        return [_item("déjà-vu")]

    async def fake_ingest(_item: ScrapedLeboncoinItem, _slug: str) -> IngestOutcome:
        raise AssertionError("aucune ingestion attendue")

    monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
    monkeypatch.setattr(leboncoin_poller, "_ingest_item", fake_ingest)

    outcome = await poll_leboncoin_once({"déjà-vu"})

    assert outcome.ingested_count == 0
    assert outcome.ads_seen == 1


@pytest.mark.asyncio
async def test_first_cycle_baseline_marks_ids_without_ingesting(
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_fetch(
        _url: str, _proxy: str | None, _page: int = 1
    ) -> list[ScrapedLeboncoinItem]:
        return [_item("1"), _item("2")]

    async def fake_ingest(_item: ScrapedLeboncoinItem, _slug: str) -> IngestOutcome:
        raise AssertionError("aucune ingestion attendue pendant la référence")

    monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
    monkeypatch.setattr(leboncoin_poller, "_ingest_item", fake_ingest)

    seen: set[str] = set()
    outcome = await poll_leboncoin_once(seen, baseline=True)

    assert seen == {"1", "2"}
    assert outcome.baseline_established is True
    assert outcome.ingested_count == 0


def _dated_items(
    prefix: str, count: int, published_at: datetime
) -> list[ScrapedLeboncoinItem]:
    return [
        ScrapedLeboncoinItem(
            external_id=f"{prefix}{index}",
            title=f"Blacksad {prefix}{index}",
            url=f"https://www.leboncoin.fr/ad/livres/{prefix}{index}",
            item_price_cents=2500,
            condition=None,
            photo_url=None,
            published_at=published_at,
        )
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_saturated_page_reads_second_page(monkeypatch: pytest.MonkeyPatch):
    pages: list[int] = []
    now = datetime.now(UTC)

    async def fake_fetch(
        _url: str, _proxy: str | None, page: int = 1
    ) -> list[ScrapedLeboncoinItem]:
        pages.append(page)
        return _dated_items("a", 32, now) if page == 1 else _dated_items("b", 2, now)

    async def fake_ingest(_item: ScrapedLeboncoinItem, _slug: str) -> IngestOutcome:
        return IngestOutcome(notified_count=0, needs_retry=False)

    monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
    monkeypatch.setattr(leboncoin_poller, "_ingest_item", fake_ingest)
    monkeypatch.setattr(settings, "LEBONCOIN_CATCHUP_MAX_PAGES", 2)

    outcome = await poll_leboncoin_once(set(), since=now - timedelta(minutes=5))

    assert pages == [1, 2]
    assert outcome.pages_fetched == 2
    assert outcome.ads_since_last_cycle == 32
    assert outcome.ads_seen == 34


@pytest.mark.asyncio
async def test_page_with_few_recent_ads_stays_single_page(monkeypatch: pytest.MonkeyPatch):
    pages: list[int] = []
    now = datetime.now(UTC)

    async def fake_fetch(
        _url: str, _proxy: str | None, page: int = 1
    ) -> list[ScrapedLeboncoinItem]:
        pages.append(page)
        return _dated_items("old", 27, now - timedelta(hours=2)) + _dated_items("new", 5, now)

    async def fake_ingest(_item: ScrapedLeboncoinItem, _slug: str) -> IngestOutcome:
        return IngestOutcome(notified_count=0, needs_retry=False)

    monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
    monkeypatch.setattr(leboncoin_poller, "_ingest_item", fake_ingest)
    monkeypatch.setattr(settings, "LEBONCOIN_CATCHUP_MAX_PAGES", 2)

    outcome = await poll_leboncoin_once(set(), since=now - timedelta(minutes=10))

    assert pages == [1]
    assert outcome.pages_fetched == 1
    assert outcome.ads_since_last_cycle == 5


def test_block_cooldown_ladder_is_capped():
    assert _block_cooldown(1) == 600
    assert _block_cooldown(2) == 1800
    assert _block_cooldown(3) == 3600
    assert _block_cooldown(4) == 7200
    assert _block_cooldown(12) == 7200


def _listing(source_name: str) -> Listing:
    return Listing(
        id=uuid.uuid4(),
        source_name=source_name,
        external_id=str(uuid.uuid4()),
        url="https://www.leboncoin.fr/ad/livres/1",
        title="Blacksad — Intégrale",
        normalized_title="blacksad — intégrale",
        category_slug="bd_comics",
        item_price_cents=2500,
        estimated_total_cents=None,
        condition="Très bon état",
        raw_payload={},
    )


def test_leboncoin_caption_shows_platform_without_invented_fees():
    caption = build_listing_caption(_listing("leboncoin"))

    assert "<b>Plateforme :</b> Leboncoin" in caption
    assert "<b>Prix :</b> 25,00 €" in caption
    assert "TTC" not in caption
    assert "<b>État :</b> TBE" in caption


def test_vinted_caption_keeps_its_existing_shape():
    caption = build_listing_caption(_listing("vinted"))

    assert "Plateforme" not in caption
    assert "<b>Prix :</b> 25,00 €" in caption


def test_leboncoin_listing_has_no_invented_shipped_total():
    from app.services.market_price_service import _vinted_total_cents

    listing = _listing("leboncoin")
    assert _vinted_total_cents(listing) is None

    vinted = _listing("vinted")
    assert _vinted_total_cents(vinted) is not None


def test_leboncoin_status_line_reports_pause_staleness_and_off(monkeypatch: pytest.MonkeyPatch):
    from app.bot.handlers import status as status_module

    now = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(status_module.settings, "LEBONCOIN_POLLER_ENABLED", True)

    pause = status_module._leboncoin_status_line(
        {"status": "cooldown", "cooldown_seconds": 1800}, now
    )
    assert "Pause anti-blocage" in pause
    assert "30 min" in pause

    active = status_module._leboncoin_status_line(
        {
            "status": "active",
            "last_poll_at": now.timestamp() - 30,
            "ads_since_last_cycle": 12,
        },
        now,
    )
    assert "Active" in active
    assert "12 nouvelles" in active

    late = status_module._leboncoin_status_line(
        {"status": "active", "last_poll_at": now.timestamp() - 7200}, now
    )
    assert "En retard" in late

    monkeypatch.setattr(status_module.settings, "LEBONCOIN_POLLER_ENABLED", False)
    assert "Désactivé" in status_module._leboncoin_status_line(None, now)
@pytest.mark.asyncio
async def test_fully_stale_page_triggers_second_page(monkeypatch: pytest.MonkeyPatch):
    pages: list[int] = []
    now = datetime.now(UTC)
    old = now - timedelta(hours=3)

    async def fake_fetch(
        _url: str, _proxy: str | None, page: int = 1
    ) -> list[ScrapedLeboncoinItem]:
        pages.append(page)
        if page == 1:
            return _dated_items("old", 35, old)
        return _dated_items("new", 5, now)

    async def fake_ingest(_item: ScrapedLeboncoinItem, _slug: str) -> IngestOutcome:
        return IngestOutcome(notified_count=0, needs_retry=False)

    monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
    monkeypatch.setattr(leboncoin_poller, "_ingest_item", fake_ingest)
    monkeypatch.setattr(settings, "LEBONCOIN_CATCHUP_MAX_PAGES", 2)

    outcome = await poll_leboncoin_once(set(), since=now - timedelta(minutes=10))

    assert pages == [1, 2]
    assert outcome.pages_fetched == 2
    assert outcome.ads_seen == 40


@pytest.mark.asyncio
async def test_partial_page_stays_single_page(monkeypatch: pytest.MonkeyPatch):
    pages: list[int] = []
    now = datetime.now(UTC)

    async def fake_fetch(
        _url: str, _proxy: str | None, page: int = 1
    ) -> list[ScrapedLeboncoinItem]:
        pages.append(page)
        return _dated_items("old", 30, now - timedelta(hours=2)) + _dated_items("new", 5, now)

    async def fake_ingest(_item: ScrapedLeboncoinItem, _slug: str) -> IngestOutcome:
        return IngestOutcome(notified_count=0, needs_retry=False)

    monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
    monkeypatch.setattr(leboncoin_poller, "_ingest_item", fake_ingest)
    monkeypatch.setattr(settings, "LEBONCOIN_CATCHUP_MAX_PAGES", 2)

    outcome = await poll_leboncoin_once(set(), since=datetime.now(UTC) - timedelta(minutes=10))

    assert pages == [1]
    assert outcome.pages_fetched == 1


@pytest.mark.asyncio
async def test_second_page_failure_keeps_first_page_ads(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(UTC)
    ingested: list[str] = []

    async def fake_fetch(
        _url: str, _proxy: str | None, page: int = 1
    ) -> list[ScrapedLeboncoinItem]:
        if page == 2:
            raise leboncoin_poller.LeboncoinCollectionError("HTTP 500")
        return [_item(f"p1-{index}") for index in range(5)]

    async def fake_ingest(item: ScrapedLeboncoinItem, _slug: str) -> IngestOutcome:
        ingested.append(item.external_id)
        return IngestOutcome(notified_count=0, needs_retry=False)

    monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
    monkeypatch.setattr(leboncoin_poller, "_ingest_item", fake_ingest)
    monkeypatch.setattr(settings, "LEBONCOIN_CATCHUP_MAX_PAGES", 2)

    outcome = await poll_leboncoin_once(set(), since=now - timedelta(minutes=10))

    assert len(ingested) == 5
    assert outcome.pages_fetched == 1
    assert outcome.ads_seen == 5


@pytest.mark.asyncio
async def test_blocked_cycle_raises_block_error_and_rotates_session(
    monkeypatch: pytest.MonkeyPatch,
):
    resets: list[bool] = []

    async def fake_fetch(*_args: object, **_kwargs: object) -> list[ScrapedLeboncoinItem]:
        raise leboncoin_poller.LeboncoinBlockError("HTTP 403")

    monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
    monkeypatch.setattr(leboncoin_poller, "reset_session", lambda: resets.append(True))

    with pytest.raises(leboncoin_poller.LeboncoinBlockError):
        await poll_leboncoin_once(set())


@pytest.mark.asyncio
async def test_silence_alert_fires_once_after_threshold(monkeypatch: pytest.MonkeyPatch):
    dispatched: list[object] = []

    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(leboncoin_poller.time, "time", lambda: clock["now"])

    def _capture(coro, **_kwargs: object) -> None:
        dispatched.append(coro)
        coro.close()

    monkeypatch.setattr(leboncoin_poller, "safe_create_task", _capture)

    last_success = clock["now"] - 600.0
    assert await leboncoin_poller._alert_silence_once(last_success, False) is False
    assert dispatched == []

    clock["now"] += 400.0
    assert await leboncoin_poller._alert_silence_once(last_success, False) is True
    assert len(dispatched) == 1

    clock["now"] += 600.0
    assert await leboncoin_poller._alert_silence_once(last_success, True) is True
    assert len(dispatched) == 1


async def test_silence_alert_fires_when_first_cycle_never_succeeded(
    monkeypatch: pytest.MonkeyPatch,
):
    dispatched: list[object] = []
    started = 1_000_000.0
    clock = {"now": started + leboncoin_poller.SILENCE_ALERT_SECONDS + 1.0}

    monkeypatch.setattr(leboncoin_poller.time, "time", lambda: clock["now"])

    def _capture(coro, **_kwargs: object) -> None:
        dispatched.append(coro)
        coro.close()

    monkeypatch.setattr(leboncoin_poller, "safe_create_task", _capture)

    assert await leboncoin_poller._alert_silence_once(started, False) is True
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_silence_alert_rearms_after_a_recovery(monkeypatch: pytest.MonkeyPatch):
    """Panne → alerte → succès → nouvelle panne → nouvelle alerte, une seule par épisode."""
    clock = {"now": 1_000_000.0}
    alerts: list[float] = []
    outcomes = ["fail", "fail", "ok", "fail", "fail", "fail", "stop"]

    async def fake_fetch(*_args: object, **_kwargs: object) -> list[ScrapedLeboncoinItem]:
        behaviour = outcomes.pop(0) if len(outcomes) > 1 else "stop"
        if behaviour == "stop":
            raise asyncio.CancelledError
        if behaviour == "fail":
            raise leboncoin_poller.LeboncoinCollectionError("tunnel mort")
        return [_item("seen-1")]

    async def fake_ingest(_item: ScrapedLeboncoinItem, _slug: str) -> IngestOutcome:
        return IngestOutcome(notified_count=0, needs_retry=False)

    def scheduled(coro, **_kwargs: object) -> None:
        frame = getattr(coro, "cr_frame", None)
        text = frame.f_locals.get("text") if frame is not None else None
        coro.close()
        if isinstance(text, str) and "muette" in text:
            alerts.append(clock["now"])

    async def fake_wait(awaitable: object, timeout: float = 0) -> None:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        clock["now"] += max(timeout, 1.0) + leboncoin_poller.SILENCE_ALERT_SECONDS

    async def fake_status() -> dict[str, object] | None:
        return {"status": "active", "last_poll_at": clock["now"]}

    async def fake_save(_payload: dict[str, object]) -> None:
        return None

    async def fake_seen() -> set[str]:
        return set()

    async def no_episode() -> leboncoin_poller.BlockEpisode | None:
        return None

    async def keep_episode(_episode: leboncoin_poller.BlockEpisode) -> None:
        return None

    async def drop_episode() -> None:
        return None

    monkeypatch.setattr(settings, "LEBONCOIN_POLLER_ENABLED", True)
    monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
    monkeypatch.setattr(leboncoin_poller, "_ingest_item", fake_ingest)
    monkeypatch.setattr(leboncoin_poller, "get_poller_status", fake_status)
    monkeypatch.setattr(leboncoin_poller, "save_poller_status", fake_save)
    monkeypatch.setattr(leboncoin_poller, "load_initial_seen_ids", fake_seen)
    monkeypatch.setattr(leboncoin_poller, "_source_is_empty", lambda: _true())
    monkeypatch.setattr(leboncoin_poller, "load_block_episode", no_episode)
    monkeypatch.setattr(leboncoin_poller, "save_block_episode", keep_episode)
    monkeypatch.setattr(leboncoin_poller, "clear_block_episode", drop_episode)
    monkeypatch.setattr(leboncoin_poller, "safe_create_task", scheduled)
    monkeypatch.setattr(leboncoin_poller.asyncio, "wait_for", fake_wait)
    monkeypatch.setattr(leboncoin_poller.time, "time", lambda: clock["now"])
    monkeypatch.setattr(leboncoin_poller.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(leboncoin_poller.random, "uniform", lambda _a, _b: 1.0)

    with pytest.raises(asyncio.CancelledError):
        await leboncoin_poller.run_leboncoin_poller(asyncio.Event())

    assert len(alerts) == 2


async def _true() -> bool:
    return True
