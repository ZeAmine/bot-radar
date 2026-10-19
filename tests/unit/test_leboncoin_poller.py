"""Tests unitaires du poller Leboncoin (collecte et ingestion simulées)."""

import uuid
from datetime import UTC, datetime

import pytest

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

    async def fake_fetch(_url: str, _proxy: str | None) -> list[ScrapedLeboncoinItem]:
        return [_item("1"), _item("2")]

    async def fake_ingest(item: ScrapedLeboncoinItem, _slug: str) -> IngestOutcome:
        ingested.append(item.external_id)
        return IngestOutcome(notified_count=0, needs_retry=False)

    monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
    monkeypatch.setattr(leboncoin_poller, "_ingest_item", fake_ingest)

    seen: set[str] = {"2"}
    count = await poll_leboncoin_once(seen, cycle_index=1)

    assert ingested == ["1"]
    assert count == 1
    assert "1" in seen


@pytest.mark.asyncio
async def test_poll_keeps_failed_and_retry_ids_for_next_cycle(monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(_url: str, _proxy: str | None) -> list[ScrapedLeboncoinItem]:
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
    count = await poll_leboncoin_once(seen)

    assert "boom" not in seen
    assert "retry" not in seen
    assert "ok" in seen
    # L'échec n'est pas compté ; « retry » l'est (annonce ingérée, notification différée).
    assert count == 2


@pytest.mark.asyncio
async def test_poll_with_no_new_items_does_not_ingest(monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(_url: str, _proxy: str | None) -> list[ScrapedLeboncoinItem]:
        return [_item("déjà-vu")]

    async def fake_ingest(_item: ScrapedLeboncoinItem, _slug: str) -> IngestOutcome:
        raise AssertionError("aucune ingestion attendue")

    monkeypatch.setattr(leboncoin_poller, "_fetch_items", fake_fetch)
    monkeypatch.setattr(leboncoin_poller, "_ingest_item", fake_ingest)

    assert await poll_leboncoin_once({"déjà-vu"}) == 0


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
        {"status": "active", "last_poll_at": now.timestamp() - 30}, now
    )
    assert "Active" in active

    late = status_module._leboncoin_status_line(
        {"status": "active", "last_poll_at": now.timestamp() - 7200}, now
    )
    assert "En retard" in late

    monkeypatch.setattr(status_module.settings, "LEBONCOIN_POLLER_ENABLED", False)
    assert "Désactivé" in status_module._leboncoin_status_line(None, now)
