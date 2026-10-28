"""Aperçu de création d'une série : un tome demandé, les autres annoncés.

L'incident : `add « La Passe-miroir » de Christelle Dabos` retenait quatre tomes,
dont deux jamais parus, et l'aperçu affichait « Cote indisponible / Dernière offre
connue : Non renseigné ». Ces tests fixent les trois maillons corrigés — l'admission
à la découverte, le rattachement d'un seul tome à l'alerte, et la cote nommée.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.bot.handlers.alerts_helpers import (
    creation_cote_lines,
    populate_creation_prices,
    render_series_volumes,
    requested_series_volume,
)
from app.domain.rules.cote_outcome import OffreCote
from app.providers.base import MarketOffer, MarketOffers
from app.services.book_metadata_service import BookMetadata, BookMetadataService
from tests.fakes_catalogue import patch_bnf_admission

CODES = ("9782075163743", "9782075215510", "9782075215619")


def tome(number: int, isbn: str) -> BookMetadata:
    return BookMetadata(
        title="La Passe-miroir", volume_title=f"La Passe-miroir, Tome {number}",
        volume_number=number,
        author="Christelle Dabos", publisher="Gallimard jeunesse", language="fr",
        isbn13=isbn, publication_year=2026,
    )


def row(number: int, isbn: str | None, **overrides) -> dict:
    base = {
        "number": number, "title": f"La Passe-miroir {number}", "isbn13": isbn,
        "confirmed": isbn is not None, "author": "Christelle Dabos",
        "publisher": "Gallimard jeunesse", "language": "fr", "source": "bnf",
        "publication_year": 2026, "admission": None, "publication_date": "2026-02-06",
    }
    base.update(overrides)
    return base


async def discover(metas: list[BookMetadata], **options):
    """Le verdict de découverte d'une série, sur des notices BNF simulées."""
    service = BookMetadataService()
    from app.domain.enums import ProviderQueryStatus
    from app.providers.google_books import ProviderQueryResult

    with patch.object(service, "_bnf_records_result", AsyncMock(return_value=(
        ProviderQueryResult(items=tuple(metas), status=ProviderQueryStatus.SUCCESS), None
    ))), patch.object(service, "_openlibrary_works", AsyncMock(return_value=(
        [], ProviderQueryResult()
    ))), patch.object(service, "verify_isbn", AsyncMock(side_effect=metas_by_code(metas))), \
            patch_bnf_admission(
                service, metas, series="La Passe-miroir",
                date_types={"9782075215510": "u", "9782075215619": "u"},
            ):
        return await service.lookup_series_notices(
            "La Passe-miroir", author_hint="Christelle Dabos", for_creation=True, **options,
        )


def metas_by_code(metas: list[BookMetadata]):
    by_code = {meta.isbn13: meta for meta in metas}

    async def verified(code, _title, **_options):
        return by_code.get(code)
    return verified


@pytest.mark.asyncio
async def test_only_the_proven_volume_is_confirmed_by_the_discovery():
    """Tomes 2 et 4 : la BNF les annonce, aucune date certaine ne les publie."""
    verdict = await discover([tome(n, CODES[i]) for i, n in enumerate((1, 2, 4))])

    confirmed = [volume for volume in verdict.volumes if volume.isbn13]
    refused = [volume for volume in verdict.volumes if not volume.isbn13]

    assert [volume.number for volume in confirmed] == [1]
    assert confirmed[0].isbn13 == CODES[0]
    assert confirmed[0].publication_date is not None
    assert {volume.number for volume in refused} == {2, 4}
    assert {volume.admission for volume in refused} == {"date_unproven"}
    assert {volume.declared_isbn13 for volume in refused} == {CODES[1], CODES[2]}


@pytest.mark.asyncio
async def test_an_announced_volume_is_detected_without_being_confirmed():
    """Le tome 2 reste au récapitulatif pour être nommé, mais il n'est pas retenu."""
    verdict = await discover([tome(n, CODES[i]) for i, n in enumerate((1, 2))])

    assert [volume.number for volume in verdict.volumes] == [1, 2]
    assert [volume.number for volume in verdict.volumes if volume.isbn13] == [1]
    assert verdict.volumes[1].admission == "date_unproven"


def test_the_recap_shows_the_retained_volume_then_names_the_detected_ones():
    rows = [
        row(1, CODES[0], price_lines=[
            "<b>Cote :</b> 23,69 € - Momox Shop, très bon état, port inclus",
            "<b>Prix bas constaté (1 an) :</b> 19,99 € - très bon état +, port inclus",
        ]),
        row(2, CODES[1], confirmed=False, admission="date_unproven", publication_date=None),
        row(4, CODES[2], confirmed=False, admission="date_unproven", publication_date=None),
    ]

    lines = render_series_volumes({"series_volumes": rows, "series_total": 4})
    text = "\n".join(lines)

    assert lines[0] == (
        f"📚 <b>Tome 1</b> - La Passe-miroir 1 · <code>{CODES[0]}</code>"
    )
    assert lines[1] == "<b>Parution :</b> 06/02/2026 (BNF) - paru"
    assert lines[2].startswith("<b>Cote :</b> 23,69 € - Momox Shop")
    assert "Prix bas constaté (1 an) :</b> 19,99 € - très bon état +, port inclus" in lines[3]
    assert "Tomes 2 et 4 détectés, non ajoutés - ajout sur demande explicite" in text
    # Un tome écarté n'occupe pas une fiche : il tient en une ligne, motif compris.
    assert f"<b>Tome 2</b> - La Passe-miroir 2 · {CODES[1]} · date de parution non prouvée" in text
    assert "Cote indisponible" not in text
    assert "Non renseigné" not in text
    assert text.endswith("<i>4 tomes annoncés par les notices.</i>")


def test_a_volume_without_collected_prices_says_so_without_inventing_a_price():
    lines = render_series_volumes({"series_volumes": [row(1, CODES[0])]})

    assert "<b>Cote :</b> non collectée pour cet aperçu" in lines
    assert "<b>Prix bas constaté (1 an) :</b> non collecté pour cet aperçu" in lines


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"isbn": CODES[0]}, 1),
        ({"isbn": CODES[1]}, 2),
        ({"requested_volume": 2}, 2),
        ({"name": "La Passe-miroir tome 2"}, 2),
        ({"name": "La Passe-miroir"}, 1),
    ],
)
def test_the_alert_attaches_only_the_volume_the_user_asked_for(data, expected):
    """Aucun tome n'est rattaché d'office : le demandé, et lui seul."""
    rows = [row(1, CODES[0]), row(2, CODES[1])]

    chosen = requested_series_volume(rows, {"name": "La Passe-miroir", **data})

    assert chosen is not None
    assert chosen["number"] == expected


def test_an_explicitly_requested_but_unproven_volume_attaches_nothing():
    """Tome 2 demandé, tome 2 non prouvé : mieux vaut aucun tome qu'un autre."""
    rows = [
        row(1, CODES[0]),
        row(2, CODES[1], confirmed=False, admission="date_unproven"),
    ]

    assert requested_series_volume(rows, {"name": "La Passe-miroir tome 2"}) is None
    assert requested_series_volume(rows, {"name": "La Passe-miroir", "isbn": CODES[1]}) is None


def offers(*prices: int) -> MarketOffers:
    """Une collecte qui rend une offre par source, état « très bon », port compris."""
    return MarketOffers(offers=tuple(
        MarketOffer(
            provider_name=f"source{index}", item_price_cents=price - 300,
            shipping_price_cents=300, delivered_price_cents=price,
            source_url=f"https://example.invalid/{index}", item_condition="Très bon état",
            confidence="high", is_available=True, is_historical=False, is_comparable=True,
            price_includes_shipping=False, raw_payload={"best_merchant": f"Marchand {index}"},
        )
        for index, price in enumerate(prices)
    ))


def test_the_cheapest_offer_becomes_the_cote_with_shipping_included():
    lines = creation_cote_lines(offers(2990, 2369), price_max=5000)

    assert lines[0] == "<b>Cote :</b> 23,69 € - Marchand 1, très bon état, port inclus"


def test_a_cap_below_every_offer_says_which_filter_rejected_them():
    lines = creation_cote_lines(offers(2990, 2369), price_max=2000)

    assert "offres écartées par les filtres" in lines[0]
    assert "la moins chère à 23,69 €" in lines[0]


def test_an_empty_collection_and_a_failed_one_do_not_read_the_same():
    assert creation_cote_lines(offers(), price_max=5000)[0] == (
        "<b>Cote :</b> aucune offre trouvée"
    )
    assert creation_cote_lines(None, price_max=5000)[0] == (
        "<b>Cote :</b> récupération impossible, la source n'a pas répondu"
    )


class CountingRepo:
    """Un dépôt de livres qui ne rend que ce que le test y a mis."""

    def __init__(self, books: dict[str, object] | None = None) -> None:
        self.books = books or {}
        self.read: list[str] = []

    async def get_by_isbn13(self, isbn13: str):
        self.read.append(isbn13)
        return self.books.get(isbn13)


class CachedBook:
    """La fiche d'un livre déjà coté, telle que le dépôt la rend."""

    def __init__(self, cache) -> None:
        self.market_cache = cache


@pytest.mark.asyncio
async def test_a_known_cote_is_read_from_the_database_without_collecting_again():
    """L'aperçu ne recollecte pas un prix que la base connaît déjà."""
    from datetime import UTC, datetime, timedelta

    from app.db.models.book_market_cache import BookMarketCache
    from app.domain.enums import ComparisonStatus

    now = datetime.now(UTC)
    cache = BookMarketCache(
        collection_status="complete",
        lowest_delivered_price_cents=2369,
        comparison_status=ComparisonStatus.AVAILABLE,
        checked_at=now, expires_at=now + timedelta(hours=24),
        current_offers=[{
            "provider_name": "chasse_aux_livres", "item_price_cents": 2069,
            "shipping_price_cents": 300, "delivered_price_cents": 2369,
            "source_url": "https://example.invalid/1", "currency": "EUR",
            "item_condition": "Très bon état", "confidence": "high", "is_available": True,
            "is_historical": False, "is_comparable": True, "price_includes_shipping": False,
            "raw_payload": {"best_merchant": "Momox Shop"},
        }],
    )
    repo = CountingRepo({CODES[0]: CachedBook(cache)})
    data = {"max_item_price_cents": 5000, "series_volumes": [row(1, CODES[0])]}
    collector = AsyncMock()

    await populate_creation_prices(data, repo, collector=collector)

    collector.assert_not_awaited()
    assert data["series_volumes"][0]["price_lines"][0] == (
        "<b>Cote :</b> 23,69 € - Momox Shop, très bon état, port inclus"
    )


@pytest.mark.asyncio
async def test_a_volume_never_priced_is_collected_once_for_the_preview():
    """Sans cote en base, l'aperçu collecte : c'est le cas qui affichait « Non renseigné »."""
    repo = CountingRepo()
    data = {"max_item_price_cents": 5000, "series_volumes": [row(1, CODES[0])]}
    collector = AsyncMock(return_value=offers(2369))

    await populate_creation_prices(data, repo, collector=collector)

    collector.assert_awaited_once()
    assert data["series_volumes"][0]["price_lines"] == [
        "<b>Cote :</b> 23,69 € - Marchand 0, très bon état, port inclus",
        "<b>Prix bas constaté (1 an) :</b> aucun relevé sur 12 mois",
    ]


@pytest.mark.asyncio
async def test_a_failed_collection_names_the_failure_instead_of_an_empty_price():
    repo = CountingRepo()
    data = {"max_item_price_cents": 5000, "series_volumes": [row(1, CODES[0])]}
    collector = AsyncMock(side_effect=TimeoutError())

    await populate_creation_prices(data, repo, collector=collector)

    assert data["series_volumes"][0]["price_lines"][0] == (
        "<b>Cote :</b> récupération impossible, la source n'a pas répondu"
    )
    assert "Non renseigné" not in "\n".join(data["series_volumes"][0]["price_lines"])


@pytest.mark.asyncio
async def test_only_proven_volumes_are_priced():
    """Un tome refusé n'a pas de fiche à coter : il garde son motif pour tout texte."""
    repo = CountingRepo()
    data = {"max_item_price_cents": 5000, "series_volumes": [
        row(2, CODES[1], confirmed=False, admission="date_unproven"),
    ]}
    collector = AsyncMock()

    await populate_creation_prices(data, repo, collector=collector)

    collector.assert_not_awaited()
    assert "price_lines" not in data["series_volumes"][0]


def test_the_accepted_offers_are_projected_before_reaching_the_rule():
    """La projection est le seul point qui traduit un état marchand en code."""
    from app.domain.rules.cote_outcome import select_cote_offer
    from app.services.book_market_pricing import cote_offres_from_market

    projected = cote_offres_from_market(offers(2369))

    assert projected == [OffreCote(
        marchand="Marchand 0", etat="TBE", total_cents=2369,
        url="https://example.invalid/0",
    )]
    assert select_cote_offer(projected).state == "cote_trouvee"
