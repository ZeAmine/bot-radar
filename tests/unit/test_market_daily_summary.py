"""Le bilan quotidien du marché : trois lignes, trois faits, une fois par jour.

La famille des messages de passe est morte — bilan à quatre compteurs, modes
« incrémental » et « complet », promesse d'une « prochaine passe complète »,
alertes « aucune cote » et « rafraîchissement dégradé ». Ce qui la remplace est
plus petit et plus utile : un rendez-vous quotidien à 09:00 (Paris) qui dit où en
est le marché, et des messages immédiats réservés aux faits sur lesquels on peut
agir. Ces tests fixent les deux moitiés de ce contrat.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.constants import ComparisonStatus
from app.db.models.alert import Alert
from app.db.models.alert_book import AlertBook
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.db.models.category import Category
from app.db.models.user import User
from app.db.repositories.market_price_repo import MarketPriceRepository
from app.services import market_health
from tests.database import sqlite_only_database

# --------------------------------------------------------------------------- #
# Les trois lignes
# --------------------------------------------------------------------------- #


def test_the_daily_summary_is_three_lines_of_facts():
    """Contrat : trois lignes — à jour, hors de leur rythme, nouveaux prix bas."""
    text = market_health.format_daily_market_summary(
        up_to_date=214, total=243, out_of_rhythm=29, new_lows=3,
    )
    lines = text.splitlines()
    assert len(lines) == 3, "le bilan tient en trois lignes, pas une de plus"
    assert "à jour : 214/243 livres" in lines[0]
    assert "Hors de leur rythme : 29 livre(s)" in lines[1]
    assert "Nouveaux prix bas de la veille : 3" in lines[2]


@pytest.mark.parametrize(
    ("up_to_date", "total", "out_of_rhythm"),
    [(214, 243, 29), (0, 0, 0), (276, 276, 0), (1, 300, 299)],
)
def test_the_two_published_numbers_cannot_contradict_each_other(
    up_to_date, total, out_of_rhythm,
):
    """« À jour » et « hors de leur rythme » partitionnent la même population."""
    assert up_to_date + out_of_rhythm == total


def test_the_daily_summary_publishes_no_work_counter():
    """Aucun compteur de travail de l'application : ni passes, ni lots, ni requêtes.

    C'est la raison d'être du remplacement : les nombres du bilan de passe
    décrivaient l'application, pas le marché.
    """
    text = market_health.format_daily_market_summary(
        up_to_date=214, total=243, out_of_rhythm=29, new_lows=3,
    )
    for absent in (
        "Traités", "Actualisées", "Restants", "Caches", "incrémental", "complet",
        "batch", "requêtes", "passe",
    ):
        assert absent not in text


def test_every_day_gets_its_summary_even_when_nothing_moved():
    """Un marché calme reste un fait : la ligne « 0 » n'est jamais omise."""
    text = market_health.format_daily_market_summary(
        up_to_date=10, total=10, out_of_rhythm=0, new_lows=0,
    )
    assert text.count("\n") == 2
    assert "Hors de leur rythme : 0 livre(s)" in text
    assert "Nouveaux prix bas de la veille : 0" in text


# --------------------------------------------------------------------------- #
# La lecture en base
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_summary_counts_books_up_to_date_and_out_of_their_rhythm(monkeypatch):
    """La même population sert aux deux nombres, et le retard suit le modèle."""
    from app.db.repositories.book_repo import BookRepository

    now = datetime.now(UTC)
    fresh = SimpleNamespace(market_cache=BookMarketCache(
        checked_at=now - timedelta(hours=1),
        last_successful_collection_at=now - timedelta(hours=1),
        refresh_state={"current": {"last_success_at": (now - timedelta(hours=1)).isoformat()}},
    ))
    ancienne = SimpleNamespace(market_cache=BookMarketCache(
        checked_at=now - timedelta(days=30),
        last_successful_collection_at=now - timedelta(days=30),
        refresh_state={"current": {"last_success_at": (now - timedelta(days=30)).isoformat()}},
    ))
    books = [fresh, ancienne, SimpleNamespace(market_cache=None)]

    monkeypatch.setattr(
        BookRepository, "get_books_for_active_bd_alerts", AsyncMock(return_value=books)
    )
    repo = SimpleNamespace(count_lows_observed_since=AsyncMock(return_value=4))
    monkeypatch.setattr(market_health, "MarketPriceRepository", lambda _s: repo)

    text = await market_health.build_daily_market_summary(AsyncMock())

    # Le livre sans cache n'est pas compté « hors de son rythme » : sa première
    # échéance est créée par la planification, `out_of_rhythm` ne mesure que des
    # composantes datées. Une seule population, donc, et un seul seuil.
    assert "à jour : 2/3 livres" in text, text
    assert "Hors de leur rythme : 1 livre(s)" in text
    assert "Nouveaux prix bas de la veille : 4" in text


@pytest.mark.asyncio
async def test_the_new_lows_are_looked_for_over_the_last_day(monkeypatch):
    """La fenêtre d'un nouveau prix bas est la veille, pas l'historique entier."""
    from app.db.repositories.book_repo import BookRepository

    monkeypatch.setattr(
        BookRepository, "get_books_for_active_bd_alerts", AsyncMock(return_value=[])
    )
    repo = SimpleNamespace(count_lows_observed_since=AsyncMock(return_value=0))
    monkeypatch.setattr(market_health, "MarketPriceRepository", lambda _s: repo)

    await market_health.build_daily_market_summary(AsyncMock())

    since = repo.count_lows_observed_since.await_args.kwargs["since"]
    assert datetime.now(UTC) - timedelta(hours=24, minutes=1) <= since <= \
        datetime.now(UTC) - timedelta(hours=23, minutes=59)


@pytest.mark.asyncio
async def test_a_low_observed_today_counts_and_an_old_one_does_not():
    """Le prix bas compté est celui **constaté** depuis la veille, pas recalculé.

    Un cache recalculé sans offre nouvelle garde la date de l'offre qui a établi le
    minimum : un prix bas d'il y a trois mois ne se compte donc pas comme neuf.
    """
    async with sqlite_only_database() as database:
        session = database.session
        category = Category(slug="bd_comics", label="BD", emoji="B", is_active=True)
        user = User(telegram_user_id=1, is_allowed=True)
        session.add_all([category, user])
        await session.flush()
        alert = Alert(
            user_id=user.id, category_id=category.id, name="BD", include_keywords=["bd"],
        )
        book = Book(title="Neuf", normalized_title="neuf")
        old_book = Book(title="Ancien", normalized_title="ancien")
        unwatched = Book(title="Hors alerte", normalized_title="hors alerte")
        session.add_all([alert, book, old_book, unwatched])
        await session.flush()
        session.add_all([
            AlertBook(alert_id=alert.id, book_id=book.id),
            AlertBook(alert_id=alert.id, book_id=old_book.id),
        ])
        now = datetime.now(UTC)
        session.add_all([
            BookMarketCache(
                book_id=book.id, annual_low_cents=500, annual_low_observed_at=now,
                comparison_status=ComparisonStatus.AVAILABLE,
            ),
            BookMarketCache(
                book_id=old_book.id, annual_low_cents=400,
                annual_low_observed_at=now - timedelta(days=90),
                comparison_status=ComparisonStatus.AVAILABLE,
            ),
            BookMarketCache(
                book_id=unwatched.id, annual_low_cents=300, annual_low_observed_at=now,
                comparison_status=ComparisonStatus.AVAILABLE,
            ),
        ])
        await session.commit()

        count = await MarketPriceRepository(session).count_lows_observed_since(
            since=now - timedelta(days=1),
        )

    assert count == 1, "seul le livre suivi dont le minimum a été constaté hier compte"


# --------------------------------------------------------------------------- #
# L'envoi
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_summary_is_sent_once_and_returns_its_text(monkeypatch):
    sent: list[str] = []

    async def fake_notify(text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(market_health, "notify_admins", fake_notify)
    monkeypatch.setattr(
        market_health, "build_daily_market_summary",
        AsyncMock(return_value="bilan", create=True),
    )

    assert await market_health.send_daily_market_summary() == "bilan"
    assert sent == ["bilan"]


@pytest.mark.asyncio
async def test_a_summary_that_cannot_be_built_says_nothing(monkeypatch):
    """Contrat : pas de bilan plutôt qu'un bilan d'erreur — le silence est un état."""
    sent: list[str] = []
    monkeypatch.setattr(market_health, "notify_admins", AsyncMock(side_effect=sent.append))

    import app.db.session as session_module

    class Exploding:
        async def __aenter__(self):
            raise RuntimeError("base indisponible")

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: Exploding())

    assert await market_health.send_daily_market_summary() is None
    assert sent == []


# --------------------------------------------------------------------------- #
# Le rendez-vous : 09:00, heure de Paris, une fois par jour
# --------------------------------------------------------------------------- #


def test_the_summary_is_scheduled_every_day_at_nine_paris_time():
    """Le rendez-vous est quotidien, à 09:00 heure de Paris, et rattrapé une fois.

    `coalesce` et `max_instances=1` évitent qu'un redémarrage produise plusieurs
    bilans d'affilée pour les jours manqués.
    """
    from apscheduler.triggers.cron import CronTrigger

    from app.tasks.scheduler import create_scheduler

    scheduler = create_scheduler()
    job = scheduler.get_job(market_health.MARKET_SUMMARY_JOB_ID)
    assert job is not None, "le bilan quotidien doit être planifié"
    trigger = job.trigger
    assert isinstance(trigger, CronTrigger)
    champs = {champ.name: str(champ) for champ in trigger.fields}
    assert champs["hour"] == "9"
    assert champs["minute"] == "0"
    assert str(trigger.timezone) == "Europe/Paris"
    assert job.max_instances == 1
    assert job.coalesce is True


# --------------------------------------------------------------------------- #
# Ce qui a disparu ne revient pas
# --------------------------------------------------------------------------- #


def test_the_pass_report_family_is_gone():
    """Contrat : plus de bilan de passe, plus de modes, plus de « prochaine passe ».

    Un retour de l'un de ces symboles ramènerait le bruit qu'on vient de retirer ;
    ce test est la borne qui l'empêche de se réinstaller sans qu'on le voie.
    """
    for absent in (
        "notify_incomplete_refresh",
        "notify_no_prices_found",
        "notify_high_error_ratio",
        "next_full_pass_at",
        "next_full_pass_note",
        "INCOMPLETE_NOTIFY_KEY_PREFIX",
        "INCOMPLETE_NOTIFY_TTL_SECONDS",
        "NO_PRICES_NOTIFY_KEY",
        "HIGH_ERROR_NOTIFY_KEY",
    ):
        assert not hasattr(market_health, absent), f"{absent} a été retiré"


def test_a_pass_result_no_longer_carries_a_mode():
    """Sans mode dans les messages, le champ `incremental` du résultat n'a plus d'objet."""
    result = market_health.RefreshPassResult(total=0, stats={"processed": 0})
    assert not hasattr(result, "incremental")
