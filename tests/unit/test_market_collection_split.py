"""La cote et l'historique ne partagent plus la même collecte de routine.

Une cote coûte dix à onze des douze requêtes qu'une collecte a le droit de
faire ; un historique seul en coûte deux. Les réunir, c'était payer l'historique
avec le budget de la cote et ne finir aucune des deux lectures. Ce fichier tient
le partage : une composante par collecte de routine, les deux à la création.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import (
    CollectionAttempt,
    MarketOffer,
    MarketOffers,
    requested_components,
)
from app.providers.chasse_aux_livres import ChasseAuxLivresProvider
from app.providers.registry import ProviderRegistry
from app.services.book_market_pricing import fetch_offers_for_book
from app.services.market_refresh_policy import (
    HOT_CADENCE,
    component_due,
    next_component_due,
    record_component,
    routine_component,
    scope_components,
)


def cache_since(*, cote_read_hours_ago: float | None, history_read_days_ago: float | None):
    """Cache minimal dont chaque composante dit quand elle a été relue."""
    now = datetime.now(UTC)
    cache = BookMarketCache(book_id=uuid.uuid4(), lowest_delivered_price_cents=3200)
    if history_read_days_ago is not None:
        cache.historical_checked_at = now - timedelta(days=history_read_days_ago)
    if cote_read_hours_ago is not None:
        cache.last_successful_collection_at = now - timedelta(hours=cote_read_hours_ago)
    return cache


def book_with(cache: BookMarketCache | None) -> Book:
    book = Book(
        title="Blacksad", normalized_title="blacksad", isbn13="9782344045084",
        market_cache=None,
    )
    book.__dict__["market_cache"] = cache
    return book


class Source(ChasseAuxLivresProvider):
    """Fournisseur qui note ce qu'on lui a demandé, sans rien coûter."""

    def __init__(self) -> None:
        super().__init__()
        self.demande: list[frozenset[str]] = []

    async def get_offers(self, **_kwargs) -> MarketOffers:
        self.demande.append(requested_components.get())
        return MarketOffers(
            [MarketOffer(
                provider_name=self.name, item_price_cents=3000, shipping_price_cents=0,
                delivered_price_cents=3000, source_url="https://chasse.example/3000",
                item_condition="Très bon", price_includes_shipping=True,
                observed_at=datetime.now(UTC),
            )],
            attempts=(CollectionAttempt(self.name, "current"),),
        )


async def _collecte(book: Book, *, scope: str = "routine") -> list[frozenset[str]]:
    source = Source()
    await fetch_offers_for_book(
        book, registry=ProviderRegistry(providers=[source]), scope=scope,
    )
    return source.demande


@pytest.mark.asyncio
async def test_une_collecte_de_routine_ne_demande_jamais_les_deux_composantes():
    """Même quand les deux échéances sont passées, la routine n'en demande qu'une."""
    from app.services.market_refresh_policy import collection_due, due_components

    cache = cache_since(cote_read_hours_ago=24 * 20, history_read_days_ago=35)
    assert due_components(cache, datetime.now(UTC)) == ("current", "history")
    assert collection_due(cache, datetime.now(UTC)) is True
    source = Source()

    await fetch_offers_for_book(
        book_with(cache), registry=ProviderRegistry(providers=[source]),
    )

    assert source.demande == [frozenset({"current"})]


@pytest.mark.asyncio
async def test_une_cote_fraiche_laisse_l_historique_seul():
    """L'historique a son tour : une lecture à deux requêtes, pas une cote déguisée."""
    source = Source()

    await fetch_offers_for_book(
        book_with(cache_since(cote_read_hours_ago=1, history_read_days_ago=31)),
        registry=ProviderRegistry(providers=[source]),
    )

    assert source.demande == [frozenset({"history"})]


@pytest.mark.asyncio
async def test_une_creation_demande_les_deux_composantes():
    """La création attend : cote et prix bas en une fois, comme aujourd'hui."""
    assert await _collecte(book_with(None), scope="creation") == [
        frozenset({"current", "history"}),
    ]


@pytest.mark.asyncio
async def test_une_creation_ne_redemande_pas_ce_qui_vient_d_etre_lu():
    """Une création sur un livre déjà coté il y a une heure ne redemande que l'historique.

    La garde de fraîcheur de la création n'est pas levée : redemander une cote
    fraîche coûterait dix requêtes pour un prix qu'on a déjà.
    """
    source = Source()

    await fetch_offers_for_book(
        book_with(cache_since(cote_read_hours_ago=1, history_read_days_ago=None)),
        registry=ProviderRegistry(providers=[source]),
        scope="creation",
    )

    assert source.demande == [frozenset({"history"})]


def test_la_portee_forcee_ne_lit_aucune_echeance():
    """La relance d'exploitation conteste l'état : elle demande les deux, toujours."""
    cache = cache_since(cote_read_hours_ago=0.5, history_read_days_ago=1)

    assert scope_components(cache, datetime.now(UTC), "forced") == frozenset(
        {"current", "history"}
    )
    assert scope_components(cache, datetime.now(UTC), "routine") == frozenset()


def test_aucune_composante_n_est_affamee_par_l_autre():
    """Sur deux mois, chaque composante obtient ses lectures, sans retard démesuré.

    C'est la propriété qui rend le partage tenable : la plus ancienne échéance
    passe la première, et sa lecture la fait repasser derrière l'autre. Aucune
    composante ne peut donc être privée par celle qui a le rythme le plus court.
    """
    now = datetime.now(UTC)
    cache = BookMarketCache(book_id=uuid.uuid4(), lowest_delivered_price_cents=3200)
    record_component(cache, "current", succeeded=True, now=now, price_cents=3200,
                     ceiling_cents=3000)
    record_component(cache, "history", succeeded=True, now=now)

    lues: list[str | None] = []
    retards: list[timedelta] = []
    moment = now
    while moment < now + timedelta(days=61):
        moment = next_component_due(cache, moment)
        lue = routine_component(cache, moment)
        assert lue is not None
        # Le retard se mesure à l'échéance de la composante servie.
        retards.append(moment - component_due(cache, lue, moment))
        lues.append(lue)
        record_component(cache, lue, succeeded=True, now=moment,
                         price_cents=3200, ceiling_cents=3000)

    assert lues.count("history") == 2  # un relevé mensuel, tenu deux fois
    assert lues.count("current") >= 200  # six heures de rythme sur deux mois
    # Aucune lecture n'attend plus d'un rythme de cote après son échéance — pas
    # même l'historique, dont le rythme est pourtant trente fois plus long.
    assert max(retards) <= HOT_CADENCE
