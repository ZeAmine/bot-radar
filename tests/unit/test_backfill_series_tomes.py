"""Le backfill des séries, testé sans réseau et sur une base temporaire.

Le script rejoue la découverte de série sur les alertes déjà créées. Ce qui se
vérifie ici : la **population** qu'il vise (BD actives sans ISBN), le mode
rapport qui ne doit rien écrire, l'**idempotence** d'une seconde passe, le
contrat de sortie — rapport lisible, JSON, codes de sortie — et, sur une passe
d'écriture complète, **le chemin réel** : les autorités répondent à la frontière
HTTP, les tomes se lient, leurs cotes s'écrivent et le total de série suit.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import backfill_series_tomes as bf  # noqa: E402

import app.db.session as app_session  # noqa: E402
import app.services.book_metadata_service as metadata  # noqa: E402
from app.core.constants import AlertStatus, CategorySlug  # noqa: E402
from app.db.models.alert import Alert  # noqa: E402
from app.db.models.alert_book import AlertBook  # noqa: E402
from app.db.models.book import Book  # noqa: E402
from app.db.models.book_market_cache import BookMarketCache  # noqa: E402
from app.db.models.category import Category  # noqa: E402
from app.db.models.user import User  # noqa: E402
from app.providers.base import MarketOffer  # noqa: E402
from app.providers.registry import ProviderRegistry as RealProviderRegistry  # noqa: E402
from app.services import book_market_pricing as pricing  # noqa: E402
from app.services.book_metadata_service import BookMetadata, CatalogueVerdict  # noqa: E402
from tests.database import sqlite_only_database  # noqa: E402
from tests.fakes_catalogue import BNF_RECORD_TEMPLATE, FakeCatalogueClient  # noqa: E402

# Un instant figé : un rapport doit dire quand il a été produit, et un test ne
# peut pas dépendre de l'heure à laquelle il tourne.
INSTANT = datetime(2026, 10, 4, 22, 11, 9, tzinfo=UTC)

TOME_1 = "9782369903086"
TOME_2 = "9782205051407"


def _alert(name: str = "Blacksad", alert_id: str | None = None) -> bf.SeriesAlert:
    return bf.SeriesAlert(alert_id=alert_id or str(uuid.uuid4()), name=name)


def _reports() -> dict[str, bf.BackfillReport]:
    """Un rapport de chaque forme : écrit, à lier, à jour, en échec."""
    alert = _alert()
    return {
        "written": bf.BackfillReport(alert, 1, 3, (TOME_1, TOME_2), False),
        "dry": bf.BackfillReport(alert, 1, 1, (TOME_1, TOME_2), True),
        "clean": bf.BackfillReport(alert, 2, 2, (), False),
        "error": bf.BackfillReport(alert, 0, 0, (), False, error="Boom: panne"),
    }


async def _seed_alerts(session) -> None:
    """Quatre alertes : la seule éligible est la BD active sans ISBN."""
    bd = Category(slug=CategorySlug.BD_COMICS.value, label="BD & Comics", emoji="📚")
    tech = Category(slug=CategorySlug.TECH.value, label="Tech", emoji="💻")
    user = User(telegram_user_id=42)
    session.add_all([bd, tech, user])
    await session.flush()
    session.add_all(
        [
            Alert(user_id=user.id, category_id=bd.id, name="Blacksad"),
            Alert(
                user_id=user.id,
                category_id=bd.id,
                name="Les Indes fourbes",
                isbn="9782889010851",
            ),
            Alert(
                user_id=user.id,
                category_id=bd.id,
                name="Blacksad (en pause)",
                status=AlertStatus.PAUSED,
            ),
            Alert(user_id=user.id, category_id=tech.id, name="Raspberry Pi 5"),
        ]
    )
    await session.commit()


async def test_read_eligible_alerts_selects_only_bd_series_without_isbn() -> None:
    """La population est « BD, active, sans ISBN » — et rien d'autre."""
    async with sqlite_only_database() as fresh:
        await _seed_alerts(fresh.session)
        with patch.object(bf, "AsyncSessionLocal", fresh.maker):
            alerts = await bf.read_eligible_alerts()

    assert [alert.name for alert in alerts] == ["Blacksad"]


async def test_read_eligible_alerts_honours_the_limit() -> None:
    """`--limit` borne la passe sans changer la sélection."""
    async with sqlite_only_database() as fresh:
        await _seed_alerts(fresh.session)
        with patch.object(bf, "AsyncSessionLocal", fresh.maker):
            alerts = await bf.read_eligible_alerts(limit=1)

    assert len(alerts) == 1


async def test_dry_run_lists_the_missing_tomes_without_linking() -> None:
    """Le mode rapport dit ce qui manque, et n'appelle jamais la liaison."""
    alert = _alert()
    link = AsyncMock()

    async def _linked(_alert: bf.SeriesAlert) -> set[str]:
        return {TOME_1}

    async def _discover(_alert: bf.SeriesAlert) -> list[BookMetadata]:
        return [
            BookMetadata(title="Blacksad Tome 1", isbn13=TOME_1),
            BookMetadata(title="Blacksad Tome 2", isbn13=TOME_2),
        ]

    reports = await bf.backfill(
        [alert], dry_run=True, linked=_linked, discover=_discover, link=link
    )

    assert reports[0].candidate_isbns == (TOME_2,)
    assert reports[0].linked_after == reports[0].linked_before == 1
    link.assert_not_awaited()
    assert bf.exit_code(reports, dry_run=True) == bf.EXIT_REMAINING


async def test_a_second_pass_links_nothing_new() -> None:
    """L'idempotence : une seconde passe rapporte zéro tome et sort en 0."""
    alert = _alert()
    linked: set[str] = set()

    async def _linked(_alert: bf.SeriesAlert) -> set[str]:
        return set(linked)

    async def _link(_alert: bf.SeriesAlert) -> None:
        linked.update({TOME_1, TOME_2})

    async def _discover(_alert: bf.SeriesAlert) -> list[BookMetadata]:
        return []

    first = await bf.backfill(
        [alert], dry_run=False, linked=_linked, discover=_discover, link=_link
    )
    assert set(first[0].candidate_isbns) == {TOME_1, TOME_2}
    assert first[0].linked_after == 2

    second = await bf.backfill(
        [alert], dry_run=False, linked=_linked, discover=_discover, link=_link
    )
    assert second[0].candidate_isbns == ()
    assert second[0].linked_after == 2
    assert bf.exit_code(second, dry_run=False) == bf.EXIT_CLEAN


async def test_a_failing_alert_does_not_stop_the_following_one() -> None:
    """Un échec sur une série laisse la suivante se faire, et sort en erreur."""
    bad, good = _alert("Blacksad"), _alert("Blast")
    linked: set[str] = set()

    async def _linked(_alert: bf.SeriesAlert) -> set[str]:
        return set(linked)

    async def _link(alert: bf.SeriesAlert) -> None:
        if alert.name == "Blacksad":
            raise RuntimeError("BNF muette")
        linked.add(TOME_1)

    async def _discover(_alert: bf.SeriesAlert) -> list[BookMetadata]:
        return []

    reports = await bf.backfill(
        [bad, good], dry_run=False, linked=_linked, discover=_discover, link=_link
    )

    assert reports[0].error is not None
    assert reports[1].candidate_isbns == (TOME_1,)
    assert bf.exit_code(reports, dry_run=False) == bf.EXIT_ERROR


async def test_dry_run_on_a_temporary_database_renders_a_readable_report(
    capsys,
) -> None:
    """Le script tourne sur une base temporaire, écrit zéro livre, et se lit."""
    async with sqlite_only_database() as fresh:
        await _seed_alerts(fresh.session)
        fake_service = MagicMock()
        # Un double fidèle : le service rend un **verdict**, pas une liste nue. Rendre
        # une liste ici ferait passer `discover_tomes` sans jamais l'exercer, et le
        # mode rapport casserait en production dès que le verdict arriverait (ADR-041).
        fake_service.lookup_series_notices = AsyncMock(
            return_value=CatalogueVerdict(
                consulted=("bnf",),
                notices=(BookMetadata(title="Blacksad Tome 2", isbn13=TOME_2),),
            )
        )
        args = argparse.Namespace(dry_run=True, json=False, limit=None)
        with (
            patch.object(bf, "AsyncSessionLocal", fresh.maker),
            patch.object(bf, "BookMetadataService", MagicMock(return_value=fake_service)),
        ):
            code = await bf.run(args)

        written = await fresh.session.scalar(select(func.count()).select_from(Book))

    assert code == bf.EXIT_REMAINING
    assert written == 0
    out = capsys.readouterr().out
    assert "Blacksad" in out
    assert "À LIER" in out
    assert TOME_2 in out


async def test_a_write_run_links_through_the_ingestion_call() -> None:
    """En mode écriture, la liaison passe par `link_books_from_bnf(..., series=True)`."""
    async with sqlite_only_database() as fresh:
        await _seed_alerts(fresh.session)
        args = argparse.Namespace(dry_run=False, json=False, limit=None)
        with (
            patch.object(bf, "AsyncSessionLocal", fresh.maker),
            patch.object(bf, "link_books_from_bnf", AsyncMock()) as link,
        ):
            code = await bf.run(args)

    assert code == bf.EXIT_CLEAN
    link.assert_awaited_once()
    assert link.await_args.kwargs["series"] is True


async def test_an_unreadable_population_is_an_error(capsys) -> None:
    """Une base illisible sort en 2, et le dit sur la sortie d'erreur."""
    args = argparse.Namespace(dry_run=True, json=False, limit=None)
    with patch.object(bf, "read_eligible_alerts", AsyncMock(side_effect=RuntimeError("down"))):
        code = await bf.run(args)

    assert code == bf.EXIT_ERROR
    assert "Lecture des alertes impossible" in capsys.readouterr().err


def test_json_carries_the_header_the_isbns_and_the_totals() -> None:
    """La sortie machine porte l'en-tête, les ISBN et les totaux, pas un tableau nu."""
    reports = _reports()
    payload = json.loads(
        bf.as_json(list(reports.values()), generated_at=INSTANT, dry_run=False)
    )

    assert payload["genere_le"].startswith("2026-10-04")
    assert payload["mode"] == "ecriture"
    assert payload["population"]["eligibles"] == len(reports)
    assert payload["alertes"][0]["isbns"] == [TOME_1, TOME_2]
    assert payload["total"]["tomes_ajoutes"] == 2
    assert payload["alertes"][-1]["erreur"] == "Boom: panne"


def test_exit_code_distinguishes_error_pending_and_clean() -> None:
    """Le code de sortie dit la vérité de chaque passe."""
    reports = _reports()

    assert bf.exit_code([reports["error"]], dry_run=False) == bf.EXIT_ERROR
    assert bf.exit_code([reports["dry"]], dry_run=True) == bf.EXIT_REMAINING
    assert bf.exit_code([reports["clean"]], dry_run=False) == bf.EXIT_CLEAN
    # Un travail réellement appliqué n'est pas une erreur : la passe a réussi.
    assert bf.exit_code([reports["written"]], dry_run=False) == bf.EXIT_CLEAN


def test_render_names_each_series_and_the_missing_tomes() -> None:
    """Le rapport lisible nomme la série, ce qui manque, et son verdict."""
    reports = _reports()

    dry = bf.render([reports["dry"]], generated_at=INSTANT, dry_run=True)
    assert "Blacksad" in dry
    assert "À LIER" in dry
    assert TOME_1 in dry
    assert TOME_2 in dry
    assert "1 alerte(s) ont des tomes à lier." in dry

    written = bf.render([reports["written"]], generated_at=INSTANT, dry_run=False)
    assert "1 → 3 tomes" in written
    assert "2 tome(s) lié(s)." in written


@pytest.mark.parametrize("json_flag", [False, True])
async def test_run_empty_population_succeeds(json_flag: bool, capsys) -> None:
    """Sans alerte éligible, le script sort en 0 avec un rapport daté."""
    args = argparse.Namespace(dry_run=True, json=json_flag, limit=None)
    with patch.object(bf, "read_eligible_alerts", AsyncMock(return_value=[])):
        code = await bf.run(args)

    assert code == bf.EXIT_CLEAN
    out = capsys.readouterr().out
    assert "2026-" in out or "genere_le" in out
    assert ("0 alerte(s) BD active(s) sans ISBN" in out) is not json_flag


# ------------------------------------------- le chemin d'écriture, pour de vrai
#
# Les tests ci-dessus injectent `link` : ils disent ce que le script décide, pas ce
# qu'il écrit. Ceux-ci exécutent la passe d'écriture entière — `link_books_from_bnf`
# comprise — en servant les autorités à leur frontière HTTP et les marchands par le
# registre du point de cotation unique. C'est le seul endroit qui vérifie de bout en
# bout ce qu'un utilisateur voit : des tomes liés avec leur ISBN-13, des cotes
# écrites, et un total de série qui vaut la somme de ces cotes.


def _ean13(body: str) -> str:
    """Complète un corps de douze chiffres par sa clé de contrôle EAN-13."""
    total = sum(
        int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(body)
    )
    return body + str((10 - total % 10) % 10)


# Trois tomes connus de la BNF, deux que seul OpenLibrary rend : le repli d'ADR-040
# passe donc par le même chemin que la source primaire.
BNF_ISBNS = [_ean13(f"978220500{number:03d}") for number in range(1, 4)]
OPENLIBRARY_ISBNS = [_ean13(f"978287100{number:03d}") for number in range(4, 6)]
MARKET_CENTS = {
    isbn: 1000 + 250 * index for index, isbn in enumerate(BNF_ISBNS + OPENLIBRARY_ISBNS)
}
LOWEST_CENTS = {isbn: cents - 200 for isbn, cents in MARKET_CENTS.items()}
EXPECTED_ISBNS = sorted(BNF_ISBNS + OPENLIBRARY_ISBNS)


def _bnf_tome(number: int, isbn: str, subtitle: str) -> str:
    return BNF_RECORD_TEMPLATE.format(
        ark=f"blacksad{number}",
        title=f"Blacksad Tome {number} : {subtitle} / Díaz Canales, Juan (1972-....)",
        creator="Díaz Canales, Juan (1972-....). Auteur du texte",
        publisher="Dargaud (Paris)",
        date=f"<dc:date>200{number}</dc:date>",
        fmt="1 vol. (48 p.) : ill. en coul. ; 32 cm",
        isbn=isbn,
    )


BNF_RECORDS = [
    _bnf_tome(1, BNF_ISBNS[0], "Quelque part entre les ombres"),
    _bnf_tome(2, BNF_ISBNS[1], "Arctic-Nation"),
    _bnf_tome(3, BNF_ISBNS[2], "Âme rouge"),
]
OPENLIBRARY_DOCS = [
    {
        "title": f"Blacksad Tome {number}",
        "author_name": ["Juan Díaz Canales"],
        "publisher": ["Dargaud"],
        "first_publish_year": 2000 + number,
        "number_of_pages_median": 48,
        "language": ["fre"],
        "isbn": [isbn],
    }
    for number, isbn in zip((4, 5), OPENLIBRARY_ISBNS, strict=True)
]


class _FakeRegistry:
    """Registre marchand : une offre d'occasion par tome, plus bas 1 an compris."""

    # Tracés sur la classe : le point de cotation crée une instance par livre,
    # ces traces s'éteindraient avec elle.
    calls: ClassVar[list[str | None]] = []
    last_context: ClassVar[tuple[str, str | None, str | None]] = ("", None, None)

    async def fetch_all_offers(
        self,
        *,
        isbn13: str | None,
        title: str,
        author: str | None = None,
        edition: str | None = None,
    ) -> list[MarketOffer]:
        # La signature reproduit le vrai registre : le contexte est lu par le test.
        _FakeRegistry.calls.append(isbn13)
        _FakeRegistry.last_context = (title, author, edition)
        price = MARKET_CENTS[str(isbn13)]
        return [
            MarketOffer(
                provider_name="Librairie Test",
                item_price_cents=price,
                shipping_price_cents=0,
                delivered_price_cents=price,
                source_url=f"https://exemple.test/{isbn13}",
                item_condition="Très bon",
                raw_payload={
                    "best_merchant": "Librairie Test",
                    "historical_low_1y_cents": LOWEST_CENTS[str(isbn13)],
                },
            )
        ]

    @staticmethod
    def select_best_offer(offers: list[MarketOffer]) -> MarketOffer | None:
        return RealProviderRegistry.select_best_offer(offers)


async def _written_state(session: Any) -> dict[str, Any]:
    """L'état écrit, relu en colonnes nues : aucune valeur gardée par la session."""
    market_cents = (
        await session.execute(select(BookMarketCache.lowest_delivered_price_cents))
    ).scalars().all()
    lowest_cents = (
        await session.execute(select(BookMarketCache.historical_low_1y_cents))
    ).scalars().all()
    return {
        "books": await session.scalar(select(func.count()).select_from(Book)),
        "links": await session.scalar(select(func.count()).select_from(AlertBook)),
        "isbns": list(
            (await session.execute(select(Book.isbn13).order_by(Book.isbn13))).scalars().all()
        ),
        "market_cents": sorted(cents for cents in market_cents if cents is not None),
        "lowest_cents": sorted(cents for cents in lowest_cents if cents is not None),
        "annual_cents": sorted((await session.scalars(
            select(BookMarketCache.annual_low_cents)
        )).all()),
        "series_total_cents": await session.scalar(select(Alert.series_total_cents)),
    }


def _write_pass_patches(fresh: Any) -> tuple[ExitStack, FakeCatalogueClient]:
    """Les trois sessions du chemin et les deux frontières (autorités, marchands)."""
    _FakeRegistry.calls.clear()
    _FakeRegistry.last_context = ("", None, None)
    fake_http = FakeCatalogueClient(
        bnf_records=BNF_RECORDS, openlibrary_docs=OPENLIBRARY_DOCS
    )
    stack = ExitStack()
    stack.enter_context(patch.object(bf, "AsyncSessionLocal", fresh.maker))
    stack.enter_context(patch.object(app_session, "AsyncSessionLocal", fresh.maker))
    stack.enter_context(patch.object(pricing, "AsyncSessionLocal", fresh.maker))
    stack.enter_context(
        patch.object(metadata, "get_shared_http_client", lambda: fake_http)
    )
    stack.enter_context(patch.object(pricing, "ProviderRegistry", _FakeRegistry))
    return stack, fake_http


@pytest.mark.asyncio
async def test_a_write_pass_links_and_prices_every_tome_of_the_series() -> None:
    """La passe réelle : tomes liés, cotes écrites, total de série somme de ces cotes."""
    async with sqlite_only_database() as fresh:
        await _seed_alerts(fresh.session)
        args = argparse.Namespace(dry_run=False, json=False, limit=None)
        stack, fake_http = _write_pass_patches(fresh)
        with stack:
            code = await bf.run(args)
        written = await _written_state(fresh.session)

    assert code == bf.EXIT_CLEAN
    assert written["isbns"] == EXPECTED_ISBNS
    assert written["links"] == len(EXPECTED_ISBNS)
    assert written["market_cents"] == sorted(MARKET_CENTS.values())
    assert written["lowest_cents"] == sorted(LOWEST_CENTS.values())
    # L'agrégat fournisseur non daté reste informatif, pas un minimum annuel prouvé.
    assert written["annual_cents"] == sorted(MARKET_CENTS.values())
    assert written["series_total_cents"] == sum(MARKET_CENTS.values())
    # Le repli a réellement servi : sans lui, deux tomes manqueraient.
    assert fake_http.openlibrary_queries
    # Une interrogation marchande par tome lié — pas une de plus, pas une de moins —
    # et le titre l'a accompagné : c'est ce repli par titre qui rend une cote
    # possible même quand l'ISBN seul ne suffit pas.
    assert sorted(call for call in _FakeRegistry.calls if call) == EXPECTED_ISBNS
    assert _FakeRegistry.last_context[0].startswith("Blacksad Tome")


@pytest.mark.asyncio
async def test_a_second_write_pass_adds_neither_book_link_nor_total() -> None:
    """Rejouer la passe ne duplique rien et laisse le total de série où il est."""
    async with sqlite_only_database() as fresh:
        await _seed_alerts(fresh.session)
        args = argparse.Namespace(dry_run=False, json=False, limit=None)
        stack, _fake_http = _write_pass_patches(fresh)
        with stack:
            first_code = await bf.run(args)
            first = await _written_state(fresh.session)
            second_code = await bf.run(args)
            second = await _written_state(fresh.session)

    assert (first_code, second_code) == (bf.EXIT_CLEAN, bf.EXIT_CLEAN)
    assert first["series_total_cents"] == sum(MARKET_CENTS.values())
    assert second == first
