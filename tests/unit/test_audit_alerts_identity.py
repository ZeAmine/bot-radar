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
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import audit_alerts_identity as audit  # noqa: E402

from app.services.book_metadata_service import (  # noqa: E402
    AUTORITE_BNF,
    AUTORITE_OPENLIBRARY,
    BookMetadata,
    IsbnVerdict,
)

ISBN_BD = "9782369903062"  # La couleur des choses, Martin Panchaud, Çà et là

# Un instant figé : un rapport doit dire quand il a été produit, et un test
# ne peut pas dépendre de l'heure à laquelle il tourne.
INSTANT = datetime(2026, 10, 4, 22, 11, 9, tzinfo=UTC)
ISBN_HOMONYME = "9782214080627"  # Les Enfants perdus, l'homonyme de 1989

NOTICE_BD = BookMetadata(
    title="La couleur des choses",
    author="Martin Panchaud",
    publisher="Çà et là",
    isbn13=ISBN_BD,
)
NOTICE_HOMONYME = BookMetadata(title="Les Enfants perdus", author="Jean Roucas")

# Même ouvrage, mais rendu par l'autorité secondaire : c'est la provenance, pas le
# contenu, que l'audit doit rendre visible.
NOTICE_HOMONYME_REPLI = BookMetadata(
    title="Les Enfants perdus", author="Jean Roucas", source="openlibrary"
)
NOTICE_REPLI = BookMetadata(
    title="La couleur des choses",
    author="Martin Panchaud",
    publisher="Çà et là",
    source="openlibrary",
)


TOUTES_AUTORITES = (AUTORITE_BNF, AUTORITE_OPENLIBRARY)


def _confirme(notice: BookMetadata) -> IsbnVerdict:
    """Une autorité a répondu, et le verdict se souvient laquelle."""
    return IsbnVerdict(outcome="confirme", notice=notice, consulted=(notice.source,))


def _inconnu(*, consultees: tuple[str, ...] = TOUTES_AUTORITES) -> IsbnVerdict:
    """Personne ne connaît ce code, après les avoir toutes consultées."""
    return IsbnVerdict(outcome="inconnu", consulted=consultees)


def _silence(motif: str, *, notice: BookMetadata | None = None) -> IsbnVerdict:
    """Personne n'a pu répondre ; un repli peut avoir parlé, il ne tranche pas."""
    return IsbnVerdict(outcome="silence", notice=notice, panne=motif, consulted=TOUTES_AUTORITES)


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
    rendu = _confirme(notice) if notice else _inconnu()
    assert [a.code for a in audit.inspect_identity(identite, rendu)] == attendus


def test_le_detail_dit_ce_que_l_autorite_a_repondu():
    """Un signal sans fait est inexploitable : le détail porte la réponse de l'autorité."""
    conflit = audit.inspect_identity(_alerte(), _confirme(NOTICE_HOMONYME))[0]
    assert "Les Enfants perdus" in conflit.detail


@pytest.mark.parametrize(
    ("notice", "attendu", "interdit"),
    [
        pytest.param(NOTICE_HOMONYME, "la BNF", "OpenLibrary", id="bnf"),
        pytest.param(NOTICE_HOMONYME_REPLI, "OpenLibrary", "la BNF", id="repli"),
    ],
)
def test_le_conflit_nome_l_autorite_qui_a_repondu(notice, attendu, interdit):
    """« la BNF » écrit en dur transformait un repli OpenLibrary en accusation BNF.

    C'est le défaut mesuré sur l'audit complet : un verdict rendu par l'autorité
    secondaire se lisait comme si la BNF avait répondu, et `La Bête` ressortait en `ok`.
    """
    detail = audit.inspect_identity(_alerte(), _confirme(notice))[0].detail

    assert detail.startswith(attendu), detail
    assert interdit not in detail, f"{interdit} n'a pas répondu, mais le détail l'accuse : {detail}"


def test_chaque_divergence_nome_l_autorite_de_la_notice():
    """Titre, auteur et éditeur nomment eux aussi l'autorité, pas « la notice »."""
    identite = _alerte(
        author="Quelquun Dautre",
        publisher="AutreMaison",
        name="La couleur des choses (édition 2025)",
    )
    divergences = audit.inspect_identity(identite, _confirme(NOTICE_REPLI))

    codes = [a.code for a in divergences]
    assert codes == [
        audit.TITRE_DIVERGENT,
        audit.AUTEUR_DIVERGENT,
        audit.EDITEUR_DIVERGENT,
    ]
    for anomalie in divergences:
        assert anomalie.detail.startswith("OpenLibrary"), anomalie.detail
        assert "la BNF" not in anomalie.detail, anomalie.detail


def test_un_isbn_inconnu_nome_les_autorites_qui_ont_repondu():
    """« Personne ne connaît » n'a de sens que si quelqu'un a vraiment été demandé."""
    anomalie = audit.inspect_identity(_alerte(), _inconnu())[0]

    assert anomalie.code == audit.ISBN_INCONNU
    assert "ni la BNF ni OpenLibrary" in anomalie.detail
    assert ISBN_BD in anomalie.detail


def test_le_message_suit_les_autorites_que_le_verdict_porte():
    """Un troisième repli entre en scène : le rapport le nomme, sans code à modifier.

    La liste venait d'une constante du script : elle serait restée « ni la BNF ni
    OpenLibrary » et aurait menti sur une consultation qui n'a pas eu lieu.
    """
    verdict = IsbnVerdict(
        outcome="inconnu",
        consulted=(AUTORITE_BNF, AUTORITE_OPENLIBRARY, "repli-du-futur"),
    )

    anomalie = audit.inspect_identity(_alerte(), verdict)[0]

    assert "ni la BNF ni OpenLibrary ni repli-du-futur" in anomalie.detail


def test_un_isbn_mal_forme_n_accuse_aucune_autorite():
    """Un code mal formé n'est soumis à personne : l'accusation serait inventée.

    `consult_authorities` court-circuite sur un ISBN invalide avant tout appel ;
    écrire « ni la BNF ni OpenLibrary ne connaissent ce code » hériterait exactement du
    défaut que ce rapport corrige, en sens inverse.
    """
    anomalie = audit.inspect_identity(_alerte(isbn="123"), _inconnu(consultees=()))[0]

    assert anomalie.code == audit.ISBN_INCONNU
    assert "aucune autorité interrogée" in anomalie.detail
    assert "la BNF" not in anomalie.detail
    assert "OpenLibrary" not in anomalie.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("notice", [NOTICE_BD, NOTICE_REPLI], ids=["bnf", "repli"])
async def test_audit_note_l_autorite_de_chaque_verdict(notice):
    """Le verdict retient qui a répondu : c'est la seule donnée qui distingue un repli."""

    async def lookup(_isbn: str) -> IsbnVerdict:
        return _confirme(notice)

    verdict = (await audit.audit([_alerte()], lookup))[0]

    assert verdict.verdict.autorite == notice.source
    assert verdict.confirmee_par_la_bnf is (notice.source == audit.AUTORITE_BNF)


@pytest.mark.asyncio
async def test_aucune_autorite_ne_repondant_n_accuse_ni_ne_confirme():
    """Les deux autorités ont répondu « rien » : c'est une absence de preuve, nommée."""

    async def lookup(_isbn: str) -> IsbnVerdict:
        return _inconnu()

    verdict = (await audit.audit([_alerte()], lookup))[0]

    assert [a.code for a in verdict.anomalies] == [audit.ISBN_INCONNU]
    assert "ni la BNF ni OpenLibrary" in verdict.anomalies[0].detail
    assert verdict.verdict.autorite is None
    assert verdict.confirmee_par_la_bnf is False


def test_audit_et_matcher_emploient_la_meme_definition_de_variante():
    """Une variante étrangère est définie une fois : le matcher filtre, l'audit signale."""
    from app.domain.rules.listing_match import carries_foreign_isbn

    assert audit.carries_foreign_isbn is carries_foreign_isbn


@pytest.mark.asyncio
async def test_une_panne_de_reseau_met_l_alerte_de_cote():
    """Une BNF injoignable ne prouve pas qu'un ISBN est faux : absence de preuve, pas preuve."""

    async def lookup(_isbn: str) -> IsbnVerdict:
        raise TimeoutError("délai dépassé")

    verdict = (await audit.audit([_alerte()], lookup))[0]

    assert verdict.anomalies == [], "une panne ne doit produire aucune anomalie"
    assert "TimeoutError" in (verdict.motif or "")


@pytest.mark.asyncio
async def test_une_notice_manquee_ne_vaut_pas_isbn_inconnu():
    """Une requête isolée qui échoue met l'alerte de côté ; seul le silence accuse.

    C'est le défaut que la panne globale ne couvrait pas : la sonde dit que la BNF
    répond, donc l'audit continuait, et `None` devenait `isbn_inconnu`.
    """

    async def lookup(_isbn: str) -> IsbnVerdict:
        return _silence("TimeoutError: délai dépassé")

    verdict = (await audit.audit([_alerte()], lookup))[0]

    assert verdict.anomalies == [], "une notice manquée n'autorise aucune accusation"
    assert verdict.motif == "autorité muette : TimeoutError: délai dépassé"


def test_le_rapport_distingue_une_alerte_impossible_dune_alerte_saine():
    """Le statut doit se lire : « non vérifiée » n'est pas « conforme », ni « en faute »."""
    muette = audit.AlertVerdict(_alerte(), verdict=_silence("statut HTTP 503"))
    rapport = audit.render(
        [muette, _conforme()],
    audit_le=INSTANT,
)

    assert "autorité muette : statut HTTP 503" in rapport
    assert "non vérifiée" in rapport
    assert "conforme" not in rapport, "une alerte non vérifiée ne valide pas le rapport"


@pytest.mark.asyncio
async def test_la_pause_separe_deux_notices_et_ne_les_precede_pas():
    """La pause existe pour ménager l'autorité ; avant la première notice, elle est perdue."""
    pauses: list[float] = []

    async def note(delay: float) -> None:
        pauses.append(delay)

    async def lookup(_isbn: str) -> IsbnVerdict:
        return _confirme(NOTICE_BD)

    await audit.audit([_alerte()], lookup, pause=note)
    assert pauses == [], "une seule notice, aucune pause"

    await audit.audit([_alerte(), _alerte()], lookup, pause=note)
    assert pauses == [audit.BNF_PAUSE_SECONDS]


def test_le_rapport_nomme_une_alerte_une_seule_fois():
    """Deux anomalies ne doivent pas donner deux en-têtes : une alerte, un nom."""
    verdict = audit.AlertVerdict(
        _alerte(author="Quelquun Dautre", publisher="AutreMaison"),
        verdict=_confirme(NOTICE_REPLI),
        anomalies=[
            audit.Anomaly(audit.AUTEUR_DIVERGENT, "auteur"),
            audit.Anomaly(audit.EDITEUR_DIVERGENT, "éditeur"),
        ],
    )

    rapport = audit.render([verdict], audit_le=INSTANT)

    assert rapport.count("La couleur des choses") == 1, "en-tête dupliqué"
    assert "1 alerte(s) à regarder" in rapport
    assert "ANOMALIE" in rapport


def test_le_rapport_dit_conforme_quand_rien_ne_bouge():
    rapport = audit.render([_conforme()], audit_le=INSTANT)
    assert "conforme" in rapport
    assert "ANOMALIE" not in rapport


def test_le_mode_discret_oublie_les_alertes_saines():
    saines = [_conforme(), _conforme()]
    rapport = audit.render(saines, quiet=True, audit_le=INSTANT)
    assert "conforme" in rapport
    assert "ok " not in rapport


def test_un_repli_ne_ressemble_pas_a_un_ok():
    """La ligne d'un repli nomme l'autorité qui a répondu et l'absence de la BNF."""
    rapport = audit.render(
        [audit.AlertVerdict(_alerte(), verdict=_confirme(NOTICE_REPLI))], audit_le=INSTANT
    )

    assert "ok " not in rapport, "un repli ne doit pas se lire comme une confirmation"
    assert "REPLI" in rapport
    assert "OpenLibrary a répondu, pas la BNF" in rapport
    assert "conforme" not in rapport, "un rapport contenant un repli n'est pas conforme"


@pytest.mark.asyncio
async def test_une_notice_de_repli_apres_panne_est_non_verifiee():
    """La BNF muette ne se remplace pas par OpenLibrary : personne n'a tranché.

    C'est le second point de la mission : la panne isolée ne doit jamais se lire comme
    une réponse. Ici, non plus, elle ne doit pas se déguiser en repli.
    """

    async def lookup(_isbn: str) -> IsbnVerdict:
        return _silence("TimeoutError: délai dépassé", notice=NOTICE_REPLI)

    verdict = (await audit.audit([_alerte()], lookup))[0]

    assert verdict.anomalies == [], "une panne n'autorise aucune accusation"
    assert verdict.verdict.autorite is None, "aucune autorité n'a tranché"
    assert "TimeoutError" in (verdict.motif or "")
    assert "repli écarté" in (verdict.motif or "")


def test_un_repli_apres_panne_est_compte_comme_non_verifie_pas_comme_repli():
    """Les deux décomptes doivent rester disjoints : c'est ce qui les rend lisibles."""
    rapport = audit.render(
        [
            audit.AlertVerdict(
                _alerte(),
                verdict=_silence("TimeoutError: délai dépassé", notice=NOTICE_REPLI),
            )
        ],
    audit_le=INSTANT,
)

    assert "1 alerte(s) non vérifiées" in rapport
    assert "non confirmée(s) par la BNF" not in rapport
    assert "conforme" not in rapport


def test_un_verdict_sans_autorite_n_est_plus_dit_conforme():
    """Régression : « aucune autorité a répondu » surmontait « conforme aux notices »."""
    rapport = audit.render(
        [audit.AlertVerdict(_alerte(), verdict=_inconnu())], audit_le=INSTANT
    )

    assert "aucune autorité a répondu" in rapport
    assert "conforme" not in rapport, "un rapport qui doute de son autorité n'est pas conforme"


def test_le_repli_survit_au_mode_discret():
    """Le mode discret retire le bruit `ok`, pas la seule trace qui reste à lire."""
    rapport = audit.render(
        [_conforme(), audit.AlertVerdict(_alerte(), verdict=_confirme(NOTICE_REPLI))],
        quiet=True,
        audit_le=INSTANT,
    )

    assert "REPLI" in rapport
    assert "OpenLibrary a répondu, pas la BNF" in rapport
    assert "ok " not in rapport
    assert "conforme" not in rapport


def test_le_compte_final_separe_les_replis_des_anomalies():
    """Un repli est compté même quand il porte déjà une anomalie : les deux sont vrais."""
    verdict = audit.AlertVerdict(
        _alerte(),
        verdict=_confirme(NOTICE_HOMONYME_REPLI),
        anomalies=audit.inspect_identity(_alerte(), _confirme(NOTICE_HOMONYME_REPLI)),
    )

    rapport = audit.render([verdict], audit_le=INSTANT)

    assert "1 alerte(s) à regarder" in rapport
    assert "1 alerte(s) non confirmée(s) par la BNF — réponse de : OpenLibrary" in rapport
    assert "OpenLibrary rattache cet ISBN à" in rapport, "l'anomalie nomme aussi l'autorité"
    assert "conforme" not in rapport


@pytest.mark.asyncio
async def test_un_isbn_inconnu_n_est_pas_un_repli():
    """Personne n'ayant répondu, il n'y a pas de repli à compter deux fois.

    Le décompte mélangeait « OpenLibrary a répondu » et « personne n'a répondu », et
    concluait « réponse de : OpenLibrary, aucune autorité » pour une alerte déjà
    signalée comme `isbn_inconnu`.
    """

    async def lookup(_isbn: str) -> IsbnVerdict:
        return _inconnu()

    rapport = audit.render(await audit.audit([_alerte()], lookup), audit_le=INSTANT)

    assert "isbn_inconnu" in rapport
    assert "non confirmée(s) par la BNF" not in rapport
    assert "aucune autorité" not in rapport


def test_une_alerte_muette_n_est_pas_comptee_comme_un_repli():
    """Sans réponse, il n'y a pas d'autorité à nommer : le double compte mentirait."""
    rapport = audit.render(
        [audit.AlertVerdict(_alerte(), verdict=_silence("statut HTTP 503"))],
        audit_le=INSTANT,
    )

    assert "non confirmée(s) par la BNF" not in rapport
    assert "1 alerte(s) non vérifiées" in rapport


def test_le_json_expose_la_meme_information():
    verdict = audit.AlertVerdict(
        _alerte(),
        verdict=_inconnu(),
        anomalies=[audit.Anomaly(audit.ISBN_CONFLIT, "d")],
    )

    payload = json.loads(audit.as_json([verdict], audit_le=INSTANT))
    alerte = payload["alertes"][0]

    assert alerte["isbn"] == ISBN_BD
    assert alerte["anomalies"][0]["code"] == audit.ISBN_CONFLIT
    assert alerte["unverified_reason"] is None
    assert alerte["autorite"] is None


def test_le_json_expose_l_autorite_de_chaque_verdict():
    """Sans ce champ, le `--json` et le texte ne diraient pas la même chose."""
    payload = json.loads(
        audit.as_json(
            [
                audit.AlertVerdict(_alerte(), verdict=_confirme(NOTICE_REPLI)),
                audit.AlertVerdict(_alerte(inactive=True), verdict=_confirme(NOTICE_BD)),
            ],
            audit_le=INSTANT,
        )
    )

    assert [verdict["autorite"] for verdict in payload["alertes"]] == ["openlibrary", "bnf"]
    assert payload["population"] == {"auditees": 2, "inactives": 1}


# --- L'en-tête : l'instant et la population ---


def _conforme(*, inactive: bool = False) -> audit.AlertVerdict:
    """Une alerte que la BNF elle-même a confirmée : la ligne `ok` du rapport."""
    return audit.AlertVerdict(_alerte(inactive=inactive), verdict=_confirme(NOTICE_BD))


def test_l_entete_nome_l_instant_et_la_population():
    """Un rapport qui ne dit pas sur quoi il a été produit n'est opposable à personne."""
    rapport = audit.render([_conforme(), _conforme(inactive=True)], audit_le=INSTANT)

    assert "Audit d'identité — 2026-10-04 22:11:09 UTC" in rapport
    assert "Population auditée : 2 alerte(s) porteuse(s) d'un ISBN (dont 1 inactive(s))" in rapport


def test_l_en_compte_les_inactives_seulement_si_leur_lecture_est_dem():
    """Hors `--include-inactive`, la population lue n'en contient aucune."""
    rapport = audit.render([_conforme()], audit_le=INSTANT)

    assert "(dont 0 inactive(s))" in rapport


def test_deux_rapports_sur_des_populations_differentes_ne_se_confondent_pas():
    """Même commande, base qui a grandi entre les deux : les deux rapports le montrent."""
    avant = audit.render([_conforme()], audit_le=INSTANT)
    apres = audit.render([_conforme() for _ in range(3)], audit_le=INSTANT)

    assert "Population auditée : 1 alerte(s)" in avant
    assert "Population auditée : 3 alerte(s)" in apres
    assert audit.render_entete([_conforme()], INSTANT) != audit.render_entete(
        [_conforme(), _conforme(inactive=True)], INSTANT
    )


def test_le_mode_discret_garde_l_entete():
    """`--quiet` retire les lignes `ok`, pas ce qui permet de lire le rapport."""
    verdicts = [
        _conforme(),
        audit.AlertVerdict(
            _alerte(inactive=True),
            verdict=_inconnu(),
            anomalies=[audit.Anomaly(audit.ISBN_INCONNU, "personne ne connaît cet ISBN")],
        ),
    ]

    rapport = audit.render(verdicts, quiet=True, audit_le=INSTANT)

    assert "Audit d'identité — 2026-10-04 22:11:09 UTC" in rapport
    assert "Population auditée : 2 alerte(s) porteuse(s) d'un ISBN (dont 1 inactive(s))" in rapport
    assert "1 alerte(s) à regarder" in rapport


def test_le_json_et_le_texte_disent_le_meme_chose():
    """Le `--json` doit porter le même instant et le même effectif que le texte."""
    verdicts = [
        _conforme(),
        audit.AlertVerdict(
            _alerte(inactive=True),
            verdict=_inconnu(),
            anomalies=[audit.Anomaly(audit.ISBN_INCONNU, "personne")],
        ),
    ]

    payload = json.loads(audit.as_json(verdicts, audit_le=INSTANT))
    rapport = audit.render(verdicts, quiet=True, audit_le=INSTANT)

    assert audit.format_instant(datetime.fromisoformat(payload["genere_le"])) in rapport
    assert payload["population"] == {"auditees": 2, "inactives": 1}
    assert f"Population auditée : {payload['population']['auditees']} alerte(s)" in rapport
    assert len(payload["alertes"]) == payload["population"]["auditees"]


def test_un_rapport_vide_reste_identifiable():
    """Zéro alerte reste un rapport : sans en-tête, on ne saurait pas quand il a été fait."""
    rapport = audit.render([], audit_le=INSTANT)

    assert "Audit d'identité — 2026-10-04 22:11:09 UTC" in rapport
    assert "Population auditée : 0 alerte(s) porteuse(s) d'un ISBN (dont 0 inactive(s))" in rapport


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

    async def consult_authorities(self, _isbn: str) -> IsbnVerdict:
        return _confirme(self.notice) if self.notice else _inconnu()


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
async def test_un_repli_laisse_le_code_de_sortie_a_zero(monkeypatch):
    """Décision du produit : un repli n'est pas une anomalie d'identité.

    Il se distingue dans la ligne, le résumé, le compte final et le JSON ; le code
    de sortie reste réservé aux anomalies, pour que rien ne change à qui l'automatise.
    """
    _hors_ligne(monkeypatch)
    monkeypatch.setattr(audit, "read_identities", _depot([_alerte()]))
    monkeypatch.setattr(audit, "BookMetadataService", lambda: _Moteur(NOTICE_REPLI))
    assert await audit.run(_args()) == audit.EXIT_CLEAN == 0


@pytest.mark.asyncio
async def test_aucune_autorite_ne_repondant_rend_le_code_1(monkeypatch):
    """Deux silences sont une accusation : le code 1 doit dire qu'il y a à regarder."""
    _hors_ligne(monkeypatch)
    monkeypatch.setattr(audit, "read_identities", _depot([_alerte()]))
    monkeypatch.setattr(audit, "BookMetadataService", lambda: _Moteur(None))
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
    """La panne ne doit pas se déguiser en « ISBN inconnu », ni en succès silencieux.

    `lookup_by_isbn` absorbant ses propres erreurs, une BNF injoignable renverrait
    `None` pour chaque alerte — donc `isbn_inconnu` partout. Le code 2 dit que
    l'audit n'a pas eu lieu ; le code 0 dirait qu'il n'y avait rien à trouver.
    """
    _hors_ligne(monkeypatch)
    monkeypatch.setattr(audit, "read_identities", _depot([_alerte()]))
    monkeypatch.setattr(audit, "bnf_joignable", _joignable(False))

    class Interdit:
        async def consult_authorities(self, _isbn: str) -> IsbnVerdict:
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
async def test_deux_executions_sur_une_base_qui_change_donnent_deux_effectifs(
    monkeypatch, capsys
):
    """Le cas qui a motivé l'en-tête : le bot crée des alertes pendant que l'audit tourne.

    Deux « 0 anomalie » de dénominateurs différents ne se comparent pas ; deux en-têtes
    qui les nomment, si. L'instant, lui, tombe à la seconde : deux exécutions dans la
    même seconde partagent donc leur première ligne, et c'est l'effectif qui les départage.
    """
    _hors_ligne(monkeypatch)
    lectures = iter([[_alerte()], [_alerte(), _alerte(), _alerte(inactive=True)]])

    async def lire(**_kwargs: object) -> list[audit.AlertIdentity]:
        return next(lectures)

    monkeypatch.setattr(audit, "read_identities", lire)
    monkeypatch.setattr(audit, "BookMetadataService", lambda: _Moteur(NOTICE_BD))

    assert await audit.run(_args(include_inactive=True)) == audit.EXIT_CLEAN
    premier = capsys.readouterr().out
    assert await audit.run(_args(include_inactive=True)) == audit.EXIT_CLEAN
    second = capsys.readouterr().out

    assert "Population auditée : 1 alerte(s) porteuse(s) d'un ISBN (dont 0 inactive(s))" in premier
    assert "Population auditée : 3 alerte(s) porteuse(s) d'un ISBN (dont 1 inactive(s))" in second
    # Les deux « ligne 2 » — l'effectif — diffèrent ; la ligne 1, l'instant, peut non.
    assert premier.splitlines()[1] != second.splitlines()[1]


@pytest.mark.asyncio
async def test_un_rapport_sur_base_vide_reste_date_et_parse_en_json(monkeypatch, capsys):
    """Une base vide est un rapport comme un autre : daté en texte, valide en JSON."""
    _hors_ligne(monkeypatch)
    monkeypatch.setattr(audit, "read_identities", _depot([]))

    assert await audit.run(_args(json=True)) == audit.EXIT_CLEAN
    payload = json.loads(capsys.readouterr().out)

    assert payload["population"] == {"auditees": 0, "inactives": 0}
    assert payload["alertes"] == []
    assert payload["genere_le"].endswith("+00:00")

    assert await audit.run(_args()) == audit.EXIT_CLEAN
    texte = capsys.readouterr().out

    assert "Population auditée : 0 alerte(s) porteuse(s) d'un ISBN (dont 0 inactive(s))" in texte
    assert "rien à confronter" in texte


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
