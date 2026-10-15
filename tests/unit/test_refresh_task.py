"""Tests unitaires pour la tâche planifiée de rafraîchissement des prix BD."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.constants import ComparisonStatus, MatchConfidence
from app.db.models.book import Book
from app.db.models.book_market_cache import BookMarketCache
from app.providers.base import MarketOffer, MarketOffers
from app.tasks.refresh_book_prices import _run_wave, refresh_book_prices_task


@pytest.fixture(autouse=True)
def synthetic_health_transports(monkeypatch):
    """Cette suite double la tâche ; les sondes SQL sont vérifiées en intégration."""
    client = MagicMock()
    client.set = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.market_health.get_redis_client", lambda: client)
    monkeypatch.setattr("app.tasks.refresh_book_prices.notify_stale_certified_lows", AsyncMock())
    monkeypatch.setattr("app.tasks.refresh_book_prices.notify_silent_providers", AsyncMock())
    monkeypatch.setattr(
        "app.tasks.refresh_book_prices.notify_stale_current_collections", AsyncMock(),
    )
    monkeypatch.setattr("app.tasks.refresh_book_prices.resolve_scheduled_catalogues", AsyncMock())


@pytest.mark.asyncio
async def test_refresh_book_prices_task_success():
    """Vérifie l'exécution de la tâche de rafraîchissement avec mise à jour du cache."""
    mock_book = Book(
        id=uuid.uuid4(),
        title="Blacksad — Intégrale",
        normalized_title="blacksad integrale",
        isbn13="9782205078054",
    )

    mock_offer = MarketOffer(
        provider_name="ebay",
        item_price_cents=1500,
        shipping_price_cents=450,
        delivered_price_cents=1950,
        source_url="https://ebay.fr/123",
        confidence=MatchConfidence.HIGH,
    )

    mock_registry = MagicMock()
    mock_registry.fetch_all_offers = AsyncMock(return_value=[mock_offer])
    mock_registry.select_best_offer = MagicMock(return_value=mock_offer)

    mock_session = AsyncMock()

    with pytest.MonkeyPatch.context() as mp:
        mock_book_repo = MagicMock()
        mock_book_repo.get_books_for_active_bd_alerts = AsyncMock(return_value=[mock_book])

        mock_market_repo = MagicMock()
        mock_market_repo.refresh_annual_references = AsyncMock()
        mock_market_repo.save_snapshot = AsyncMock()
        mock_market_repo.get_cache_by_book_id = AsyncMock(return_value=None)
        mock_market_repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])
        # Le dépôt simulé rend la cote qu'il viendrait d'écrire : la passe la relit
        # pour ventiler ses statistiques.
        mock_market_repo.update_or_create_cache = AsyncMock(
            return_value=BookMarketCache(
                id=uuid.uuid4(),
                book_id=mock_book.id,
                lowest_delivered_price_cents=1950,
                comparison_status=ComparisonStatus.AVAILABLE,
            )
        )

        mp.setattr("app.tasks.refresh_book_prices.BookRepository", lambda _s: mock_book_repo)
        mp.setattr(
            "app.services.book_market_pricing.MarketPriceRepository", lambda _s: mock_market_repo
        )

        stats = await refresh_book_prices_task(registry=mock_registry, session=mock_session)

        assert stats["processed"] == 1
        assert stats["available"] == 1
        assert stats["errors"] == 0

        mock_market_repo.save_snapshot.assert_called_once_with(mock_book.id, mock_offer)
        mock_market_repo.update_or_create_cache.assert_called_once_with(
            book_id=mock_book.id,
            lowest_delivered_price_cents=1950,
            best_provider_name="ebay",
            best_source_url="https://ebay.fr/123",
            confidence=MatchConfidence.HIGH,
            comparison_status=ComparisonStatus.AVAILABLE,
            cache_duration_hours=24,
            historical_low_1y_cents=None,
            observed_at=mock_offer.observed_at,
        )


@pytest.mark.asyncio
async def test_refresh_book_prices_task_incremental():
    """Vérifie que le mode incrémental interroge uniquement le micro-lot prioritaire."""
    mock_book = Book(
        id=uuid.uuid4(),
        title="1629",
        normalized_title="1629",
        isbn13="9782344045084",
    )
    mock_registry = MagicMock()
    mock_registry.fetch_all_offers = AsyncMock(return_value=[])
    mock_registry.select_best_offer = MagicMock(return_value=None)
    mock_session = AsyncMock()

    with pytest.MonkeyPatch.context() as mp:
        mock_book_repo = MagicMock()
        mock_book_repo.get_books_needing_price_refresh = AsyncMock(return_value=[mock_book])
        mock_book_repo.get_books_for_active_bd_alerts = AsyncMock(return_value=[])

        mock_market_repo = MagicMock()
        mock_market_repo.refresh_annual_references = AsyncMock()
        mock_market_repo.save_snapshot = AsyncMock()
        mock_market_repo.get_cache_by_book_id = AsyncMock(return_value=None)
        mock_market_repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])
        mock_market_repo.update_or_create_cache = AsyncMock()

        mp.setattr("app.tasks.refresh_book_prices.BookRepository", lambda _s: mock_book_repo)
        mp.setattr(
            "app.services.book_market_pricing.MarketPriceRepository", lambda _s: mock_market_repo
        )

        stats = await refresh_book_prices_task(
            registry=mock_registry, session=mock_session, incremental=True, batch_size=5
        )

        assert stats["processed"] == 1
        mock_book_repo.get_books_needing_price_refresh.assert_called_once_with(limit=5)
        mock_book_repo.get_books_for_active_bd_alerts.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_keeps_existing_price_when_provider_is_silent():
    """Un silence réseau ne doit pas effacer un prix déjà connu et encore frais."""
    mock_book = Book(
        id=uuid.uuid4(),
        title="Blacksad",
        normalized_title="blacksad",
        isbn13="9782344045084",
    )
    mock_registry = MagicMock()
    mock_registry.fetch_all_offers = AsyncMock(return_value=[])
    mock_registry.select_best_offer = MagicMock(return_value=None)

    existing_cache = MagicMock()
    existing_cache.lowest_delivered_price_cents = 470
    existing_cache.comparison_status = ComparisonStatus.AVAILABLE
    existing_cache.historical_low_1y_cents = 900

    with pytest.MonkeyPatch.context() as mp:
        mock_book_repo = MagicMock()
        mock_book_repo.get_books_for_active_bd_alerts = AsyncMock(return_value=[mock_book])

        mock_market_repo = MagicMock()
        mock_market_repo.refresh_annual_references = AsyncMock()
        mock_market_repo.save_snapshot = AsyncMock()
        mock_market_repo.get_cache_by_book_id = AsyncMock(return_value=existing_cache)
        mock_market_repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])
        mock_market_repo.update_or_create_cache = AsyncMock()

        mp.setattr("app.tasks.refresh_book_prices.BookRepository", lambda _s: mock_book_repo)
        mp.setattr(
            "app.services.book_market_pricing.MarketPriceRepository", lambda _s: mock_market_repo
        )

        stats = await refresh_book_prices_task(
            registry=mock_registry, session=AsyncMock(), concurrency=1
        )

    assert stats["processed"] == 1
    assert stats["available"] == 0
    assert stats["unavailable"] == 1
    mock_market_repo.update_or_create_cache.assert_not_called()
    mock_market_repo.refresh_annual_references.assert_awaited_once_with(
        [mock_book.id], force=True
    )
    assert existing_cache.historical_low_1y_cents == 900
    assert existing_cache.checked_at is not None


@pytest.mark.asyncio
async def test_refresh_processes_several_books_in_one_wave():
    """Une vague traite tous ses livres, y compris ceux qui lèvent une exception."""
    books = [
        Book(id=uuid.uuid4(), title=f"Livre {index}", normalized_title=f"livre {index}")
        for index in range(3)
    ]
    mock_registry = MagicMock()
    mock_registry.fetch_all_offers = AsyncMock(side_effect=[[], RuntimeError("timeout"), []])
    mock_registry.select_best_offer = MagicMock(return_value=None)

    # Sans ce garde-fou, l'alerte « aucune cote » part pour de vrai : c'est le
    # vrai bot (token du .env local) qui l'envoie, et la fenêtre de
    # dédoublonnage Redis (`redis://redis`, injoignable hors compose) renvoie
    # « slot libre » à chaque passe — d'où un message à chaque pytest.
    sends: list[str] = []

    async def faux_notify(text: str) -> None:
        sends.append(text)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.services.market_health.notify_admins", faux_notify)
        client = MagicMock()
        client.set = AsyncMock(return_value=True)
        mp.setattr("app.services.market_health.get_redis_client", lambda: client)

        mock_book_repo = MagicMock()
        mock_book_repo.get_books_for_active_bd_alerts = AsyncMock(return_value=books)

        mock_market_repo = MagicMock()
        mock_market_repo.refresh_annual_references = AsyncMock()
        mock_market_repo.save_snapshot = AsyncMock()
        mock_market_repo.get_cache_by_book_id = AsyncMock(return_value=None)
        mock_market_repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])
        mock_market_repo.update_or_create_cache = AsyncMock()

        mp.setattr("app.tasks.refresh_book_prices.BookRepository", lambda _s: mock_book_repo)
        mp.setattr(
            "app.services.book_market_pricing.MarketPriceRepository", lambda _s: mock_market_repo
        )

        stats = await refresh_book_prices_task(
            registry=mock_registry, session=AsyncMock(), concurrency=3
        )

    assert stats["processed"] == 3
    assert stats["errors"] == 1
    assert stats["unavailable"] == 2

    # L'alerte reste produite : seule sa destination change.
    alertes = [s for s in sends if "Collecte de prix en échec" in s]
    assert len(alertes) == 1, "la passe complète doit signaler l'absence totale de cote"
    assert "Passe complet (3 livres)" in alertes[0]
    assert "3 livres traités, 0 cote disponible" in alertes[0]



# --- L'alerte « aucune cote », exercée par la tâche et non par son aide ---------


def _livres(n: int) -> list[Book]:
    return [
        Book(id=uuid.uuid4(), title=f"Livre {i}", normalized_title=f"livre {i}",
             isbn13=f"97823699030{i:02d}")
        for i in range(n)
    ]


async def _passe_sans_offres(
    mp: pytest.MonkeyPatch,
    *,
    incremental: bool,
    livres: list[Book],
    sends: list[str],
    fenetre_ouverte: bool = True,
) -> dict[str, int]:
    """Fait tourner la tâche sur des livres que personne ne cote."""
    mock_registry = MagicMock()
    mock_registry.fetch_all_offers = AsyncMock(return_value=[])
    mock_registry.select_best_offer = MagicMock(return_value=None)

    book_repo = MagicMock()
    book_repo.get_books_needing_price_refresh = AsyncMock(return_value=livres)
    book_repo.get_books_for_active_bd_alerts = AsyncMock(return_value=livres)

    market_repo = MagicMock()
    market_repo.refresh_annual_references = AsyncMock()
    market_repo.save_snapshot = AsyncMock()
    market_repo.get_cache_by_book_id = AsyncMock(return_value=None)
    market_repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])
    market_repo.update_or_create_cache = AsyncMock(return_value=BookMarketCache(
        comparison_status=ComparisonStatus.UNAVAILABLE,
    ))

    mp.setattr("app.tasks.refresh_book_prices.BookRepository", lambda _s: book_repo)
    mp.setattr("app.services.book_market_pricing.MarketPriceRepository", lambda _s: market_repo)

    async def faux_notify(text: str) -> None:
        sends.append(text)

    mp.setattr("app.services.market_health.notify_admins", faux_notify)
    # Fenêtre ouverte : chaque passe tente l'envoi, c'est le test qui compte.
    # La pause anti-rate-limit est neutralisée : elle est réelle et sans
    # rapport avec ce que ces tests observent.
    client = MagicMock()
    client.set = AsyncMock(return_value=fenetre_ouverte)
    mp.setattr("app.services.market_health.get_redis_client", lambda: client)
    # Les deux autres contrôles de santé sont couverts ailleurs ; ici ils ne
    # doivent pas bruiter le comptage.
    mp.setattr("app.tasks.refresh_book_prices.notify_stale_certified_lows", AsyncMock())
    mp.setattr("app.tasks.refresh_book_prices.asyncio.sleep", AsyncMock())
    mp.setattr("app.tasks.refresh_book_prices.notify_silent_providers", AsyncMock())

    return await refresh_book_prices_task(
        registry=mock_registry, session=AsyncMock(), incremental=incremental, batch_size=8
    )


@pytest.mark.asyncio
async def test_passe_incremental_sans_cote_n_alerte_personne():
    """Le scénario exact du message reçu : 8 livres, aucune cote, aucune alerte.

    Le lot incrémental ne couvre pas la sélection : le dire « toutes les sources
    sont muettes » serait accuser quatre fournisseurs sur huit livres, alors que
    la base en compte des dizaines.
    """
    sends: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        stats = await _passe_sans_offres(mp, incremental=True, livres=_livres(8), sends=sends)

    assert stats["processed"] == 8
    assert stats["available"] == 0
    assert [s for s in sends if "Collecte de prix en échec" in s] == []


@pytest.mark.asyncio
async def test_passe_complete_sans_offre_ne_signale_pas_un_incident():
    """La passe complète, elle, a le droit de conclure — et ne le fait qu'une fois."""
    sends: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        premiere = await _passe_sans_offres(
            mp, incremental=False, livres=_livres(12), sends=sends
        )
        # Deuxième passe identique, fenêtre Redis fermée : rien ne doit partir.
        seconde = await _passe_sans_offres(
            mp,
            incremental=False,
            livres=_livres(12),
            sends=sends,
            fenetre_ouverte=False,
        )

    assert premiere["processed"] == 12
    assert seconde["processed"] == 12
    alertes = [s for s in sends if "Collecte de prix en échec" in s]
    assert alertes == [], "une absence normale d'offres n'est pas un incident"


@pytest.mark.asyncio
async def test_provider_failure_is_degraded_not_a_book_error():
    """Un fournisseur muet n'est pas une exception : la cote du livre n'a pas planté.

    C'est le 5/8 du message « Rafraîchissement dégradé ». La collecte a répondu
    « cette source a échoué, aucune offre », et le compteur d'erreurs le lisait
    comme un livre cassé — assez pour franchir le seuil à chaque lot.
    """
    livres = _livres(8)
    sends: list[str] = []

    async def _passe(mp: pytest.MonkeyPatch, *, fenetre_ouverte: bool) -> dict[str, int]:
        registry = MagicMock()
        registry.failed_provider_names = set()
        registry.fetch_all_offers = AsyncMock(
            return_value=MarketOffers(failed_providers=("chasse_aux_livres",))
        )
        registry.select_best_offer = MagicMock(return_value=None)

        book_repo = MagicMock()
        book_repo.get_books_needing_price_refresh = AsyncMock(return_value=livres)
        market_repo = MagicMock()
        market_repo.refresh_annual_references = AsyncMock()
        market_repo.save_snapshot = AsyncMock()
        market_repo.get_cache_by_book_id = AsyncMock(return_value=None)
        market_repo.get_alert_ids_linked_to_book = AsyncMock(return_value=[])
        market_repo.update_or_create_cache = AsyncMock(
            return_value=BookMarketCache(comparison_status=ComparisonStatus.UNAVAILABLE)
        )
        mp.setattr("app.tasks.refresh_book_prices.BookRepository", lambda _s: book_repo)
        mp.setattr("app.services.book_market_pricing.MarketPriceRepository", lambda _s: market_repo)

        async def faux_notify(text: str) -> None:
            sends.append(text)

        mp.setattr("app.services.market_health.notify_admins", faux_notify)
        client = MagicMock()
        client.set = AsyncMock(return_value=fenetre_ouverte)
        mp.setattr("app.services.market_health.get_redis_client", lambda: client)
        mp.setattr("app.tasks.refresh_book_prices.notify_stale_certified_lows", AsyncMock())
        mp.setattr("app.tasks.refresh_book_prices.notify_silent_providers", AsyncMock())
        mp.setattr("app.tasks.refresh_book_prices.asyncio.sleep", AsyncMock())
        return await refresh_book_prices_task(
            registry=registry, session=AsyncMock(), incremental=True, batch_size=8
        )

    with pytest.MonkeyPatch.context() as mp:
        premiere = await _passe(mp, fenetre_ouverte=True)
        seconde = await _passe(mp, fenetre_ouverte=False)

    assert premiere["errors"] == 0
    assert premiere["degraded"] == 8
    assert seconde["errors"] == 0
    alertes = [s for s in sends if "Rafraîchissement dégradé" in s]
    assert len(alertes) == 1
    assert "chasse_aux_livres" in alertes[0]


# --- « Traité » n'est pas « mis à jour » ---


class _CacheEcrit:
    """Un cache dont la collecte a réellement abouti à une cote utilisable."""

    collection_status = "complete"
    comparison_status = ComparisonStatus.AVAILABLE
    failed_provider_names: list[str] = []


class _CacheSansCote:
    """Un cache écrit, mais sans cote utilisable : la collecte n'a rien apporté."""

    collection_status = "empty"
    comparison_status = ComparisonStatus.UNAVAILABLE
    failed_provider_names: list[str] = []


@pytest.mark.asyncio
async def test_only_a_written_cote_counts_as_updated():
    """Le compteur de mises à jour suit l'écriture, pas l'interrogation.

    « Traités » compte les livres passés en revue ; « mis à jour » ne compte que
    ceux dont la collecte a laissé une cote utilisable. Confondre les deux faisait
    dire au bilan que tout allait bien alors que rien n'avait été écrit — c'est
    exactement ce qui s'était produit pendant trois semaines.
    """
    stats = {
        "processed": 0, "refreshed": 0, "available": 0,
        "unavailable": 0, "degraded": 0, "errors": 0,
    }

    with pytest.MonkeyPatch.context() as mp:
        compteur = {"n": 0}

        async def fake_price_book_now(_book_id, **_kwargs):
            compteur["n"] += 1
            return _CacheEcrit() if compteur["n"] % 2 else _CacheSansCote()

        mp.setattr(
            "app.tasks.refresh_book_prices.price_book_now", fake_price_book_now
        )
        livres = _livres(4)
        semaphore = asyncio.Semaphore(1)
        registry = MagicMock()
        registry.failed_provider_names = set()
        await _run_wave(livres, registry, None, semaphore, stats)

    # Les quatre livres ont été traités…
    assert stats["processed"] == 4
    # … mais deux seulement ont écrit une cote.
    assert stats["refreshed"] == 2


@pytest.mark.asyncio
async def test_a_failed_collection_is_never_counted_as_updated():
    """Un échec de collecte ne compte jamais comme une mise à jour.

    Le livre a bien été traité — il compte dans `processed` —, mais sa collecte
    n'a rien écrit : le compter comme mis à jour ferait disparaître l'incident
    derrière un chiffre rassurant.
    """
    from app.tasks.refresh_book_prices import _cote_was_written

    class Echec:
        comparison_status = ComparisonStatus.UNAVAILABLE
        collection_status = "failed"
        failed_provider_names = ["chasse_aux_livres"]

    assert _cote_was_written(Echec()) is False
    assert _cote_was_written(None) is False
    assert _cote_was_written(_CacheSansCote()) is False
    assert _cote_was_written(_CacheEcrit()) is True
