"""Contrôle de la jonction conteneur → tunnel, sur des sorties de commande doublées."""

from __future__ import annotations

import pytest

from scripts.check_junction_topology import (
    EXIT_MISMATCH,
    EXIT_OK,
    EXIT_UNKNOWN,
    ProbeError,
    check,
    firewall_rule_present,
    main,
    read_network,
    read_permitlisten,
    verify,
)

SUBNET = "172.30.0.0/16"
GATEWAY = "172.30.0.1"
AUTRE_GATEWAY = "172.31.0.1"


def _runner(reponses: dict[str, tuple[int, str, str]]):
    """Rend un exécutable doublé, qui répond selon le préfixe du script."""

    def run(script: str) -> tuple[int, str, str]:
        for prefixe, reponse in reponses.items():
            if script.startswith(prefixe):
                return reponse
        raise AssertionError(f"script inattendu : {script}")

    return run


def _run_complet(permitlisten: str = GATEWAY, regle_ok: bool = True, subnet: str = SUBNET):
    return _runner(
        {
            "docker network inspect": (0, f"{subnet} {GATEWAY}\n", ""),
            "sshd -T": (0, f"permitlisten {permitlisten}:1080\n", ""),
            "iptables -C": (0, "", "") if regle_ok else (1, "", "règle absente"),
        }
    )


def test_reseau_lu_en_deux_parties():
    assert read_network(_run_complet()) == (SUBNET, GATEWAY)


@pytest.mark.parametrize("sortie", ["", f"{SUBNET}\n", f"{SUBNET} {GATEWAY} extra\n"])
def test_reseau_incomplet_est_refuse(sortie):
    """Une sortie partielle ne doit pas produire une paire inventée."""
    run = _runner({"docker network inspect": (0, sortie, "")})

    with pytest.raises(ProbeError):
        read_network(run)


def test_permitlisten_lu_sans_port():
    assert read_permitlisten(_run_complet()) == GATEWAY


def test_permitlisten_sur_un_autre_port_est_refuse():
    run = _runner({"sshd -T": (0, f"permitlisten {GATEWAY}:2222\n", "")})

    with pytest.raises(ProbeError, match="port"):
        read_permitlisten(run)


def test_permitlisten_absent_est_refuse():
    run = _runner({"sshd -T": (0, "", "")})

    with pytest.raises(ProbeError, match="absent"):
        read_permitlisten(run)


def test_regle_presente_quand_iptables_repond_zero():
    assert firewall_rule_present(_run_complet(), SUBNET, GATEWAY) is True


def test_regle_absente_quand_iptables_repond_non_zero():
    assert firewall_rule_present(_run_complet(regle_ok=False), SUBNET, GATEWAY) is False


def test_jonction_conforme_quand_les_trois_concordent():
    assert check(GATEWAY, GATEWAY, True) == []


def test_permitlisten_sur_une_ancienne_gateway_est_un_ecart():
    """C'est le cas d'une recréation de réseau : le tunnel vise une adresse morte."""
    ecarts = check(AUTRE_GATEWAY, GATEWAY, True)

    assert len(ecarts) == 1
    assert "permitlisten" in ecarts[0]


def test_regle_absente_est_un_ecart():
    ecarts = check(GATEWAY, GATEWAY, False)

    assert len(ecarts) == 1
    assert "pare-feu" in ecarts[0]


def test_les_deux_ecarts_sont_rendus_ensemble():
    assert len(check(AUTRE_GATEWAY, GATEWAY, False)) == 2


def test_verify_conclut_au_conforme():
    assert verify(_run_complet()) == (True, [])


def test_verify_conclut_a_l_ecart():
    conforme, ecarts = verify(_run_complet(permitlisten=AUTRE_GATEWAY))

    assert conforme is False
    assert ecarts


def test_verify_ne_conclut_pas_si_le_releve_echoue():
    """Ne pas avoir pu lire n'est ni conforme ni en écart."""
    run = _runner({"docker network inspect": (1, "", "docker indisponible")})

    conforme, ecarts = verify(run)

    assert conforme is None
    assert ecarts


def test_codes_de_sortie_distincts():
    """Les trois issues doivent rester distinguables par un script appelant."""
    assert len({EXIT_OK, EXIT_MISMATCH, EXIT_UNKNOWN}) == 3


def test_main_rend_le_code_du_verdict(monkeypatch, capsys):
    """`main` traduit le verdict en code de sortie, sans dépendre du terrain."""
    monkeypatch.setattr(
        "scripts.check_junction_topology.default_runner", _run_complet(permitlisten=AUTRE_GATEWAY)
    )

    assert main([]) == EXIT_MISMATCH
    assert "EN ÉCART" in capsys.readouterr().out


def test_main_rend_zero_quand_tout_concorde(monkeypatch):
    monkeypatch.setattr("scripts.check_junction_topology.default_runner", _run_complet())

    assert main([]) == EXIT_OK
