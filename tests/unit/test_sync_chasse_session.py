"""La capture du jeton Chasse-aux-livres : le jeton n'est jamais pushé sans preuve."""

import argparse
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import sync_chasse_session as scs  # noqa: E402


def _args(**kwargs):
    base = {"duih": "", "duih_file": "", "print_only": False, "api_url": "", "token": ""}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_token_is_read_from_every_documented_source(tmp_path):
    """Les trois entrées annoncées dans l'aide doivent exister."""
    assert scs.read_token(_args(duih="  jeton  ")) == "jeton"

    path = tmp_path / "jeton.txt"
    path.write_text("depuis-fichier\n", encoding="utf-8")
    assert scs.read_token(_args(duih_file=str(path))) == "depuis-fichier"

    with patch.dict("os.environ", {"CHASSE_AUX_LIVRES_DUIH": "depuis-env"}):
        assert scs.read_token(_args()) == "depuis-env"


@pytest.mark.asyncio
async def test_validation_uses_the_real_provider_path():
    """Le test porte sur le code de production, pas sur une requête recopiée."""
    from app.providers.base import MarketOffer

    offer = MarketOffer(
        provider_name="chasse_aux_livres",
        item_price_cents=0,
        shipping_price_cents=None,
        delivered_price_cents=0,
        source_url="https://www.chasse-aux-livres.fr/prix/1/x",
        item_condition="Occasion indisponible",
        raw_payload={"historical_low_1y_cents": 1350},
    )
    provider = MagicMock()
    provider.return_value.get_offers = AsyncMock(return_value=[offer])

    with patch.object(scs, "ChasseAuxLivresProvider", provider):
        assert await scs.validate_token("un-jeton") == 1350
        provider.return_value.get_offers.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_token_that_answers_nothing_is_refused():
    """Un jeton « installé » mais inutile reproduirait exactement le silence à déplorer."""
    provider = MagicMock()
    provider.return_value.get_offers = AsyncMock(return_value=[])

    with patch.object(scs, "ChasseAuxLivresProvider", provider):
        assert await scs.validate_token("jeton-perime") is None


@pytest.mark.asyncio
async def test_a_silent_probe_falls_back_to_the_next_one():
    """Une sonde morte ne doit pas condamner un jeton pourtant valide.

    Si l'ouvrage-sonde disparaît du site, la validation échouerait à chaque
    passe et le cron repousserait un jeton qui marche — le silence reviendrait
    alors qu'on a précisément le remède en main.
    """
    from app.providers.base import MarketOffer

    offer = MarketOffer(
        provider_name="chasse_aux_livres",
        item_price_cents=0,
        shipping_price_cents=None,
        delivered_price_cents=0,
        source_url="https://www.chasse-aux-livres.fr/prix/1/x",
        item_condition="Occasion indisponible",
        raw_payload={"historical_low_1y_cents": 1290},
    )
    provider = MagicMock()
    provider.return_value.get_offers = AsyncMock(side_effect=[[], [offer]])

    with patch.object(scs, "ChasseAuxLivresProvider", provider):
        assert await scs.validate_token("un-jeton") == 1290

    assert provider.return_value.get_offers.await_count == 2


@pytest.mark.asyncio
async def test_every_probe_is_tried_before_refusing_a_token():
    """Le refus ne doit tomber qu'après toutes les sondes, jamais après la première."""
    provider = MagicMock()
    provider.return_value.get_offers = AsyncMock(return_value=[])

    with patch.object(scs, "ChasseAuxLivresProvider", provider):
        assert await scs.validate_token("jeton-perime") is None

    assert provider.return_value.get_offers.await_count == len(scs.PROBE_CANDIDATES)


@pytest.mark.asyncio
async def test_validation_restores_the_configured_token():
    """Essayer un jeton ne doit pas laisser l'environnement de production modifié."""
    from app.core.config import settings

    settings.CHASSE_AUX_LIVRES_DUIH = "celui-de-l-env"
    provider = MagicMock()
    provider.return_value.get_offers = AsyncMock(return_value=[])

    with patch.object(scs, "ChasseAuxLivresProvider", provider):
        await scs.validate_token("jeton-essai")

    assert settings.CHASSE_AUX_LIVRES_DUIH == "celui-de-l-env"


def test_main_refuses_to_inject_an_unvalidated_token(monkeypatch):
    """Aucun appel réseau quand la validation échoue : le jeton ne part pas."""
    import asyncio

    monkeypatch.setattr(sys, "argv", ["sync_chasse_session.py", "--duih", "jeton"])
    with patch.object(scs, "validate_token", AsyncMock(return_value=None)):
        assert asyncio.run(scs.main()) == 1


def test_main_injects_only_after_a_positive_validation(monkeypatch):
    """Le contrat complet : valider, puis pousser — jamais l'inverse."""
    import asyncio

    pushed: list[str] = []

    monkeypatch.setattr(scs, "validate_token", AsyncMock(return_value=1350))
    def fake_push(_api_url: str, _token: str, duih: str) -> bool:
        pushed.append(duih)
        return True

    monkeypatch.setattr(scs, "push_token_to_radar", fake_push)
    # Un jeton validé mais sans token interne n'est pas injecté : le contrat
    # Includes l'authentification, pas seulement la validation.
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync_chasse_session.py", "--duih", "jeton", "--token", "interne"],
    )

    assert asyncio.run(scs.main()) == 0
    assert pushed == ["jeton"]


def test_no_token_means_an_explanation_not_a_crash(monkeypatch):
    """Sortir par un code, pas par une exception : l'utilisateur doit savoir quoi faire."""
    import asyncio

    monkeypatch.setattr(sys, "argv", ["sync_chasse_session.py"])
    assert asyncio.run(scs.main()) == 2
