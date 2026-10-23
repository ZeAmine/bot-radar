"""Premier contact Leboncoin : aucun réseau réel, aucune donnée de page publiée.

Chaque cas correspond à une ligne de la grille de lecture de
`docs/operations/leboncoin-first-contact.md`. Le test le plus important est
`test_aucun_chemin_ne_fait_une_seconde_requete` : une seule requête est la
condition même du mandat.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from leboncoin_first_contact import (
    EXIT_NO_MANDATE,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    MANDATE_ENV,
    MISSING_DEPENDENCY_MESSAGE,
    PROXY_ENV,
    ProxyRefusalError,
    categorie_depuis_erreur,
    code_http_depuis_erreur,
    decrire_proxy,
    executer,
    main,
    valider_proxy,
)

PROXY = "socks5h://exemple:1080"
HTML_AVEC_ANNONCES = "<html><body>annonces</body></html>"


def _fetch_ok(html: str = HTML_AVEC_ANNONCES):
    appels: list[str | None] = []

    def fetch(proxy_url: str | None = None) -> str:
        appels.append(proxy_url)
        return html

    fetch.appels = appels  # type: ignore[attr-defined]
    return fetch


def _parse(n: int):
    def parse(_html: str) -> list[object]:
        return [object() for _ in range(n)]

    return parse


def _parse_leve():
    def parse(_html: str) -> list[object]:
        raise ValueError("html inattendu")

    return parse


@pytest.mark.parametrize(
    "url",
    ["socks5h://hote:1080", "socks5h://utilisateur@hote:1080"],
)
def test_les_schemas_socks5h_sont_acceptes(url: str):
    assert valider_proxy(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "socks5://hote:1080",
        "http://hote:1080",
        "https://hote:1080",
        "",
        "   ",
        "socks5h://",
    ],
)
def test_les_autres_schemas_sont_refuses(url: str):
    with pytest.raises(ProxyRefusalError):
        valider_proxy(url)


def test_un_proxy_absent_est_refuse():
    with pytest.raises(ProxyRefusalError, match="absent"):
        valider_proxy(None)


def test_le_proxy_est_masque_dans_la_sortie():
    """L'adresse réelle ne doit jamais apparaître, même en simulation."""
    assert "exemple" not in decrire_proxy("socks5h://exemple:1080")


def test_un_echange_reussi_rend_les_compteurs():
    rapport = executer(PROXY, fetch=_fetch_ok(), parse=_parse(12))

    assert rapport.categorie == "reponse"
    assert rapport.code_http == 200
    assert rapport.parsing == "reussi"
    assert rapport.annonces == 12
    assert rapport.challenge is False
    assert rapport.duree_s is not None


def test_une_page_sans_annonce_est_un_succes_parse_mais_zero_annonce():
    rapport = executer(PROXY, fetch=_fetch_ok(), parse=_parse(0))

    assert rapport.categorie == "zero_annonce"
    assert rapport.annonces == 0


def test_un_challenge_servi_en_200_est_un_challenge():
    """`fetch_search_page` lève `HTTP 200` quand le corps porte le marqueur.

    Le scraper teste le marqueur **et** le statut ; un challenge servi en 200
    arrive donc comme une `LeboncoinBlockError("HTTP 200")`. La lire comme un
    blocage rangerait un captcha dans la mauvaise ligne de la grille.
    """
    from app.services.leboncoin_scraper import LeboncoinBlockError

    def fetch(proxy_url: str | None = None) -> str:  # noqa: ARG001
        raise LeboncoinBlockError("HTTP 200")

    rapport = executer(PROXY, fetch=fetch, parse=_parse(0))

    assert rapport.categorie == "challenge"
    assert rapport.challenge is True
    assert rapport.code_http == 200


@pytest.mark.parametrize("statut", [403, 429])
def test_les_codes_de_blocage_restent_des_blocages(statut: int):
    from app.services.leboncoin_scraper import LeboncoinBlockError

    def fetch(proxy_url: str | None = None) -> str:  # noqa: ARG001
        raise LeboncoinBlockError(f"HTTP {statut}")

    rapport = executer(PROXY, fetch=fetch, parse=_parse(0))

    assert rapport.categorie == "blocage"
    assert rapport.challenge is False
    assert rapport.code_http == statut


def test_curl_cffi_indisponible_a_sa_propre_categorie():
    """Dépendance absente n'est pas un échec réseau : la cause est locale."""
    from app.services.leboncoin_scraper import LeboncoinCollectionError

    def fetch(proxy_url: str | None = None) -> str:  # noqa: ARG001
        raise LeboncoinCollectionError(MISSING_DEPENDENCY_MESSAGE)

    rapport = executer(PROXY, fetch=fetch, parse=_parse(0))

    assert rapport.categorie == "dependance_absente"
    assert rapport.categorie != "echec_reseau"


def test_un_echec_reseau_n_est_pas_deduit_d_un_message_libre():
    """Seul le message fixe de dépendance est reconnu ; le reste est réseau."""
    from app.services.leboncoin_scraper import LeboncoinCollectionError

    def fetch(proxy_url: str | None = None) -> str:  # noqa: ARG001
        raise LeboncoinCollectionError("curl_cffi indisponible, mais autre chose")

    rapport = executer(PROXY, fetch=fetch, parse=_parse(0))

    assert rapport.categorie == "echec_reseau"


def test_un_403_est_classe_blocage_avec_son_code():
    from app.services.leboncoin_scraper import LeboncoinBlockError

    def fetch(proxy_url: str | None = None) -> str:  # noqa: ARG001
        raise LeboncoinBlockError("HTTP 403")

    rapport = executer(PROXY, fetch=fetch, parse=_parse(0))

    assert rapport.categorie == "blocage"
    assert rapport.code_http == 403
    assert rapport.annonces is None


def test_un_echec_reseau_est_classe_sans_contenu():
    from app.services.leboncoin_scraper import LeboncoinCollectionError

    def fetch(proxy_url: str | None = None) -> str:  # noqa: ARG001
        raise LeboncoinCollectionError("ProxyError")

    rapport = executer(PROXY, fetch=fetch, parse=_parse(0))

    assert rapport.categorie == "echec_reseau"
    assert rapport.detail == "LeboncoinCollectionError"


def test_un_parsing_en_echec_est_signale():
    rapport = executer(PROXY, fetch=_fetch_ok(), parse=_parse_leve())

    assert rapport.parsing == "echoue"
    assert rapport.annonces is None


def test_aucun_chemin_ne_fait_une_seconde_requete():
    """Une seule requête, dans tous les cas — c'est la condition du mandat."""
    from app.services.leboncoin_scraper import LeboncoinBlockError

    fetch = _fetch_ok()
    executer(PROXY, fetch=fetch, parse=_parse(3))
    assert len(fetch.appels) == 1

    fetch = _fetch_ok()
    executer(PROXY, fetch=fetch, parse=_parse_leve())
    assert len(fetch.appels) == 1

    fetch_defaillant = _fetch_ok("<html>captcha-delivery</html>")
    executer(PROXY, fetch=fetch_defaillant, parse=_parse(0))
    assert len(fetch_defaillant.appels) == 1

    appels: list[str] = []

    def fetch_bloque_compte(proxy_url: str | None = None) -> str:  # noqa: ARG001
        appels.append("x")
        raise LeboncoinBlockError("HTTP 403")

    executer(PROXY, fetch=fetch_bloque_compte, parse=_parse(0))
    assert len(appels) == 1


def test_categories_d_erreur():
    from app.services.leboncoin_scraper import LeboncoinBlockError, LeboncoinCollectionError

    assert categorie_depuis_erreur(LeboncoinBlockError("HTTP 403")) == "blocage"
    assert categorie_depuis_erreur(LeboncoinCollectionError("x")) == "echec_reseau"
    assert categorie_depuis_erreur(RuntimeError("x")) == "echec_inattendu"


def test_code_http_lu_seulement_sur_la_forme_attendue():
    from app.services.leboncoin_scraper import LeboncoinBlockError

    assert code_http_depuis_erreur(LeboncoinBlockError("HTTP 429")) == 429
    assert code_http_depuis_erreur(LeboncoinBlockError("captcha")) is None
    assert code_http_depuis_erreur(RuntimeError("HTTP 403 <titre>")) is None


def test_le_mode_simulation_n_emet_aucune_requete(monkeypatch, capsys):
    monkeypatch.setenv(PROXY_ENV, PROXY)
    fetch = _fetch_ok()
    monkeypatch.setattr(sys, "argv", ["x"])

    code = main([], fetch=fetch, parse=_parse(1))

    sortie = capsys.readouterr().out
    assert code == EXIT_OK
    assert "mode=simulation" in sortie
    assert fetch.appels == []
    assert "--execute-once" in sortie


def test_le_mode_simulation_affiche_le_proxy_masque(monkeypatch, capsys):
    monkeypatch.setenv(PROXY_ENV, PROXY)

    main([], fetch=_fetch_ok(), parse=_parse(1))

    sortie = capsys.readouterr().out
    assert "exemple" not in sortie
    assert "hôte masqué" in sortie


def test_le_proxy_vient_de_l_environnement(monkeypatch):
    monkeypatch.setenv(PROXY_ENV, PROXY)
    monkeypatch.setenv(MANDATE_ENV, "mandat-de-test")
    fetch = _fetch_ok()

    main(["--execute-once"], fetch=fetch, parse=_parse(2))

    assert fetch.appels == [PROXY]


def test_l_option_proxy_est_refusee():
    """Le proxy ne passe que par l'environnement : pas d'argument visible en `ps`."""
    with pytest.raises(SystemExit):
        main(["--execute-once", "--proxy", PROXY], fetch=_fetch_ok(), parse=_parse(1))


def test_sans_mandat_execution_refusee_sans_aucun_appel(monkeypatch, capsys):
    """Seconde garde : `--execute-once` seul ne suffit pas."""
    monkeypatch.setenv(PROXY_ENV, PROXY)
    monkeypatch.delenv(MANDATE_ENV, raising=False)
    fetch = _fetch_ok()

    code = main(["--execute-once"], fetch=fetch, parse=_parse(1))

    assert code == EXIT_NO_MANDATE
    assert code != EXIT_OK
    assert fetch.appels == []
    assert MANDATE_ENV in capsys.readouterr().err


@pytest.mark.parametrize("valeur", ["", "   "])
def test_un_mandat_vide_est_traite_comme_absent(monkeypatch, valeur: str):
    monkeypatch.setenv(PROXY_ENV, PROXY)
    monkeypatch.setenv(MANDATE_ENV, valeur)
    fetch = _fetch_ok()

    assert main(["--execute-once"], fetch=fetch, parse=_parse(1)) == EXIT_NO_MANDATE
    assert fetch.appels == []


def test_la_simulation_ne_demande_pas_de_mandat(monkeypatch):
    """La simulation n'émet rien : elle n'a pas à exiger de mandat."""
    monkeypatch.setenv(PROXY_ENV, PROXY)
    monkeypatch.delenv(MANDATE_ENV, raising=False)
    fetch = _fetch_ok()

    assert main([], fetch=fetch, parse=_parse(1)) == EXIT_OK
    assert fetch.appels == []


def test_sans_variable_l_execution_est_refusee(monkeypatch, capsys):
    monkeypatch.delenv(PROXY_ENV, raising=False)
    fetch = _fetch_ok()

    code = main(["--execute-once"], fetch=fetch, parse=_parse(1))

    assert code == EXIT_USAGE
    assert fetch.appels == []
    assert PROXY_ENV in capsys.readouterr().err


def test_un_schema_invalide_refuse_l_execution(monkeypatch):
    monkeypatch.setenv(PROXY_ENV, "socks5://hote:1080")
    fetch = _fetch_ok()

    assert main(["--execute-once"], fetch=fetch, parse=_parse(1)) == EXIT_USAGE
    assert fetch.appels == []


def test_un_blocage_rend_un_code_de_refus(monkeypatch, capsys):
    from app.services.leboncoin_scraper import LeboncoinBlockError

    monkeypatch.setenv(PROXY_ENV, PROXY)
    monkeypatch.setenv(MANDATE_ENV, "mandat-de-test")

    def fetch(proxy_url: str | None = None) -> str:  # noqa: ARG001
        raise LeboncoinBlockError("HTTP 403")

    code = main(["--execute-once"], fetch=fetch, parse=_parse(0))

    sortie = capsys.readouterr().out
    assert code == EXIT_REFUSED
    assert "code_http=403" in sortie
    assert "categorie=blocage" in sortie


def test_la_sortie_ne_contient_aucun_contenu_de_page(monkeypatch, capsys):
    """Ni titre, ni prix, ni URL d'annonce : seuls des compteurs sortent."""
    monkeypatch.setenv(PROXY_ENV, PROXY)
    monkeypatch.setenv(MANDATE_ENV, "mandat-de-test")
    html = (
        "<html><body><a href='https://www.leboncoin.fr/ad/livres/123'>"
        "<p>Titre secret 12,50 €</p></a></body></html>"
    )

    main(["--execute-once"], fetch=_fetch_ok(html), parse=_parse(1))

    sortie = capsys.readouterr().out
    assert "Titre secret" not in sortie
    assert "12,50" not in sortie
    assert "ad/livres" not in sortie
    assert "annonces=1" in sortie
