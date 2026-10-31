"""La relance d'exploitation ne collecte que des livres nommés et valides.

Elle existe pour un état de rafraîchissement faux, pas pour interroger la source
à volonté : un ISBN qui n'est pas un ISBN-13 valide, ou un livre absent du
catalogue, s'arrêtent avant toute requête ; et une invocation ne dépasse jamais
le plafond de livres qu'elle annonce.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import refresh_book_prices  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "isbn",
    ["9782857085738", "978285708573", "2857085737", "978285708573X", "inconnu"],
)
async def test_une_saisie_qui_n_est_pas_un_isbn13_valide_est_refusee(isbn, monkeypatch):
    """Y compris un ISBN-13 faux : la clé de contrôle se vérifie avant tout appel."""

    async def interdit(*_args, **_kwargs):
        raise AssertionError("aucune collecte ne doit partir")

    monkeypatch.setattr(
        "app.services.book_market_pricing.price_book", interdit, raising=False
    )
    result = await refresh_book_prices._relance(isbn)
    assert result["relance"] == "refus"
    assert result["raison"] == "isbn_invalide"


@pytest.mark.asyncio
async def test_un_livre_absent_du_catalogue_n_est_pas_collecte(monkeypatch):
    from app.db.repositories.book_repo import BookRepository

    async def aucun_livre(_self, _isbn13):
        return None

    async def interdit(*_args, **_kwargs):
        raise AssertionError("aucune collecte ne doit partir")

    monkeypatch.setattr(BookRepository, "get_by_isbn13", aucun_livre)
    monkeypatch.setattr(
        "app.services.book_market_pricing.price_book", interdit, raising=False
    )
    result = await refresh_book_prices._relance("9782857085737")
    assert result["relance"] == "refus"
    assert result["raison"] == "livre_inconnu"


@pytest.mark.asyncio
async def test_la_relance_demande_les_deux_composantes(monkeypatch):
    """La relance demande la cote et l'historique, sans lire les échéances.

    Une collecte de routine ne demande qu'une composante et s'arrête à une
    échéance ; ce script existe pour un état qu'on juge faux, il doit donc forcer
    les deux — sinon il n'interrogerait rien et rapporterait un silence qui n'est
    que le sien.
    """
    from types import SimpleNamespace

    from app.db.repositories.book_repo import BookRepository

    vus = {}
    cache = SimpleNamespace(
        collection_status="skipped",
        lowest_delivered_price_cents=None,
        best_provider_name=None,
        best_source_url=None,
        annual_low_cents=None,
        last_attempt_at=None,
        last_successful_collection_at=None,
        next_refresh_at=None,
        source_history=None,
    )

    async def livre_cote(_self, _isbn13):
        livre = SimpleNamespace(id="livre-1", title="Un livre")
        livre.market_cache = SimpleNamespace(collection_status="partial")
        return livre

    async def collecte(_session, _book, **kwargs):
        vus["portee"] = kwargs.get("scope")
        return cache

    monkeypatch.setattr(BookRepository, "get_by_isbn13", livre_cote)
    monkeypatch.setattr("app.services.book_market_pricing.price_book", collecte)
    result = await refresh_book_prices._relance("9782857085737")
    assert vus["portee"] == "forced"
    assert result["relance"] == "refus_source"


@pytest.mark.asyncio
async def test_l_invocation_ne_depasse_pas_le_plafond_de_livres_annonce(monkeypatch):
    vus = []

    async def relance(isbn):
        vus.append(isbn)
        return {"isbn": isbn, "relance": "collecte", "etat_cote": "cote_trouvee"}

    monkeypatch.setattr(refresh_book_prices, "_relance", relance)
    code = await refresh_book_prices._run(["9782019328047", "9782011776068", "9782012388390"], 2)
    assert vus == ["9782019328047", "9782011776068"]
    assert code == 0
