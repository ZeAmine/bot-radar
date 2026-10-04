"""L'audit d'identité des alertes, testé sans base ni réseau.

La règle est une fonction pure : elle est donc vérifiée sur des cas réels, où la
notice a répondu autre chose que ce que l'alerte affirme. L'impureté — la lecture
Postgres, les 0,35 s entre notices, une panne réseau — est vérifiée séparément,
parce que c'est elle qui décide si une alerte est signalée ou mise de côté.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import audit_alerts_identity as audit  # noqa: E402

from app.services.book_metadata_service import BookMetadata  # noqa: E402

ISBN_BD = "9782369903062"  # La couleur des choses, Martin Panchaud, Çà et là
ISBN_HOMONYME = "9782214080627"  # Les Enfants perdus, l'homonyme de 1989

NOTICE_BD = BookMetadata(
    title="La couleur des choses",
    author="Martin Panchaud",
    publisher="Çà et là",
    isbn13=ISBN_BD,
)
NOTICE_HOMONYME = BookMetadata(title="Les Enfants perdus", author="Jean Roucas")


def _alerte(**kwargs) -> audit.AlertIdentity:
    defauts = {
        "alert_id": "a1b2c3d4-0000-0000-0000-000000000000",
        "name": "La couleur des choses",
        "isbn": ISBN_BD,
    }
    return audit.AlertIdentity(**{**defauts, **kwargs})


# (libellé du cas, identité, notice, codes attendus dans l'ordre)
CAS = [
    pytest.param(
        "ISBN rattaché à un autre ouvrage",
        _alerte(),
        NOTICE_HOMONYME,
        [audit.ISBN_CONFLIT],
        id="conflit",
    ),
    pytest.param(
        "ISBN que personne ne connaît",
        _alerte(),
        None,
        [audit.ISBN_INCONNU],
        id="inconnu",
    ),
    pytest.param(
        "auteur et éditeur divergents",
        _alerte(author="Quelquun Dautre", publisher="AutreMaison"),
        NOTICE_BD,
        [audit.AUTEUR_DIVERGENT, audit.EDITEUR_DIVERGENT],
        id="auteur-editeur",
    ),
    pytest.param(
        "variante portant l'ISBN d'un autre livre",
        _alerte(variants=("couleurdeschoses", f"les enfants perdus {ISBN_HOMONYME}")),
        NOTICE_BD,
        [audit.VARIANTE_ISBN_ETRANGER],
        id="variante-etrangere",
    ),
    pytest.param(
        "même œuvre, le nom de l'alerte désigne une édition",
        _alerte(name="La couleur des choses (édition 2025)"),
        NOTICE_BD,
        [audit.TITRE_DIVERGENT],
        id="titre-divergent",
    ),
    pytest.param(
        "un simple suffixe de tome ne suffit pas à diverger",
        _alerte(name="La couleur des choses - Tome 1"),
        NOTICE_BD,
        [],
        id="tome-tolere",
    ),
    pytest.param(
        "alerte propre",
        _alerte(author="Panchaud, Martin"),
        NOTICE_BD,
        [],
        id="propre",
    ),
    pytest.param(
        "variante qui répète l'ISBN de l'alerte",
        _alerte(variants=(ISBN_BD,)),
        NOTICE_BD,
        [],
        id="variante-legitime",
    ),
]


@pytest.mark.parametrize(("_cas", "identite", "notice", "attendus"), CAS)
def test_inspect_identity(_cas, identite, notice, attendus):
    """La règle, cas par cas : un code nommé par divergence, et rien de plus."""
    assert [a.code for a in audit.inspect_identity(identite, notice)] == attendus


def test_le_detail_dit_ce_que_l_autorite_a_repondu():
    """Un signal sans fait est inexploitable : le détail porte la réponse de l'autorité."""
    conflit = audit.inspect_identity(_alerte(), NOTICE_HOMONYME)[0]
    assert "Les Enfants perdus" in conflit.detail


def test_audit_et_matcher_emploient_la_meme_definition_de_variante():
    """Une variante étrangère est définie une fois : le matcher filtre, l'audit signale."""
    from app.domain.rules.listing_match import carries_foreign_isbn

    assert audit.carries_foreign_isbn is carries_foreign_isbn


@pytest.mark.asyncio
async def test_une_panne_de_reseau_met_l_alerte_de_cote():
    """Une BNF injoignable ne prouve pas qu'un ISBN est faux : absence de preuve, pas preuve."""

    async def lookup(_isbn: str) -> BookMetadata:
        raise TimeoutError("délai dépassé")

    verdict = (await audit.audit([_alerte()], lookup))[0]

    assert verdict.anomalies == [], "une panne ne doit produire aucune anomalie"
    assert "TimeoutError" in (verdict.unverified_reason or "")


@pytest.mark.asyncio
async def test_la_pause_separe_deux_notices_et_ne_les_precede_pas():
    """La pause existe pour ménager l'autorité ; avant la première notice, elle est perdue."""
    pauses: list[float] = []

    async def note(delay: float) -> None:
        pauses.append(delay)

    async def lookup(_isbn: str) -> BookMetadata:
        return NOTICE_BD

    await audit.audit([_alerte()], lookup, pause=note)
    assert pauses == [], "une seule notice, aucune pause"

    await audit.audit([_alerte(), _alerte()], lookup, pause=note)
    assert pauses == [audit.BNF_PAUSE_SECONDS]


def test_le_rapport_nomme_une_alerte_une_seule_fois():
    """Deux anomalies ne doivent pas donner deux en-têtes : une alerte, un nom."""
    verdict = audit.AlertVerdict(
        _alerte(author="Quelquun Dautre", publisher="AutreMaison"),
        anomalies=[
            audit.Anomaly(audit.AUTEUR_DIVERGENT, "auteur"),
            audit.Anomaly(audit.EDITEUR_DIVERGENT, "éditeur"),
        ],
    )

    rapport = audit.render([verdict])

    assert rapport.count("La couleur des choses") == 1, "en-tête dupliqué"
    assert "1 alerte(s) à regarder" in rapport
    assert "ANOMALIE" in rapport


def test_le_rapport_dit_conforme_quand_rien_ne_bouge():
    rapport = audit.render([audit.AlertVerdict(_alerte())])
    assert "conforme" in rapport
    assert "ANOMALIE" not in rapport


def test_le_mode_discret_oublie_les_alertes_saines():
    saines = [audit.AlertVerdict(_alerte()), audit.AlertVerdict(_alerte())]
    rapport = audit.render(saines, quiet=True)
    assert "conforme" in rapport
    assert "ok " not in rapport


def test_le_json_expose_la_meme_information():
    verdict = audit.AlertVerdict(_alerte(), anomalies=[audit.Anomaly(audit.ISBN_CONFLIT, "d")])

    payload = json.loads(audit.as_json([verdict]))

    assert payload[0]["isbn"] == ISBN_BD
    assert payload[0]["anomalies"][0]["code"] == audit.ISBN_CONFLIT
    assert payload[0]["unverified_reason"] is None


# --- Codes de sortie, en pilotant run() sans base ---


def _args(**overrides) -> argparse.Namespace:
    return argparse.Namespace(
        **{"json": False, "limit": None, "include_inactive": False, "quiet": False, **overrides}
    )


def _depot(identites: list[audit.AlertIdentity]):
    """Un faux dépôt : la lecture des alertes, sans Postgres."""

    async def lire(**_kwargs: object) -> list[audit.AlertIdentity]:
        return identites

    return lire


class _Moteur:
    """Le service de notices, réduit à ce que `run()` lui demande."""

    def __init__(self, notice: BookMetadata | None) -> None:
        self.notice = notice

    async def lookup_by_isbn(self, _isbn: str) -> BookMetadata | None:
        return self.notice

    async def close(self) -> None:
        return None


def _joignable(vrai: bool):
    async def sonde(_client: object) -> bool:
        return vrai

    return sonde


def _hors_ligne(monkeypatch) -> None:
    """Écarte Postgres, la BNF et le client partagé du chemin `run()`.

    Appelé explicitement plutôt qu'en `autouse` : la sonde d'atteignabilité est
    elle-même une fonction à vérifier, et un fixture automatique la remplacerait
    sous le test censé l'exercer.
    """

    async def fermer() -> None:
        return None

    monkeypatch.setattr(audit, "bnf_joignable", _joignable(True))
    monkeypatch.setattr(audit, "get_shared_http_client", object)
    monkeypatch.setattr(audit, "close_shared_http_client", fermer)


@pytest.mark.asyncio
async def test_code_0_quand_rien_a_signaler(monkeypatch):
    _hors_ligne(monkeypatch)
    monkeypatch.setattr(audit, "read_identities", _depot([_alerte(author="Panchaud, Martin")]))
    monkeypatch.setattr(audit, "BookMetadataService", lambda: _Moteur(NOTICE_BD))
    assert await audit.run(_args()) == audit.EXIT_CLEAN == 0


@pytest.mark.asyncio
async def test_code_1_des_une_anomalie(monkeypatch):
    _hors_ligne(monkeypatch)
    monkeypatch.setattr(audit, "read_identities", _depot([_alerte()]))
    monkeypatch.setattr(audit, "BookMetadataService", lambda: _Moteur(NOTICE_HOMONYME))
    assert await audit.run(_args()) == audit.EXIT_ANOMALIES == 1


@pytest.mark.asyncio
async def test_code_2_quand_la_base_est_illisible(monkeypatch):
    _hors_ligne(monkeypatch)
    async def boom(**_kwargs: object) -> list[audit.AlertIdentity]:
        raise OSError("connexion refusée")

    monkeypatch.setattr(audit, "read_identities", boom)
    assert await audit.run(_args()) == audit.EXIT_ERROR == 2


@pytest.mark.asyncio
async def test_une_autorite_muette_ne_produit_aucune_anomalie(monkeypatch, capsys):
    _hors_ligne(monkeypatch)
    """La panne ne doit pas se déguiser en « ISBN inconnu », ni en succès silencieux.

    `lookup_by_isbn` absorbant ses propres erreurs, une BNF injoignable renverrait
    `None` pour chaque alerte — donc `isbn_inconnu` partout. Le code 2 dit que
    l'audit n'a pas eu lieu ; le code 0 dirait qu'il n'y avait rien à trouver.
    """
    monkeypatch.setattr(audit, "read_identities", _depot([_alerte()]))
    monkeypatch.setattr(audit, "bnf_joignable", _joignable(False))

    class Interdit:
        async def lookup_by_isbn(self, _isbn: str) -> None:
            raise AssertionError("aucune notice ne doit être demandée si l'autorité est muette")

    monkeypatch.setattr(audit, "BookMetadataService", lambda: Interdit())

    assert await audit.run(_args()) == audit.EXIT_ERROR
    assert "ne répond pas" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_aucune_alerte_sans_isbn_ne_demande_rien(monkeypatch):
    _hors_ligne(monkeypatch)
    """Pas d'ISBN, pas de notice à demander : le rapport le dit et le code reste 0."""
    monkeypatch.setattr(audit, "read_identities", _depot([]))
    assert await audit.run(_args()) == audit.EXIT_CLEAN


@pytest.mark.asyncio
async def test_la_sonde_tient_compte_de_la_reponse_de_l_autorite():
    """200 dit disponible, autre chose dit muet : c'est tout ce que la sonde conclut."""

    class Reponse:
        def __init__(self, code: int) -> None:
            self.status_code = code

    class Client:
        def __init__(self, code: int, panne: bool = False) -> None:
            self.code, self.panne = code, panne

        async def get(self, *_a, **_kw) -> Reponse:
            if self.panne:
                raise TimeoutError("délai dépassé")
            return Reponse(self.code)

    assert await audit.bnf_joignable(Client(200)) is True
    assert await audit.bnf_joignable(Client(503)) is False
    assert await audit.bnf_joignable(Client(200, panne=True)) is False
