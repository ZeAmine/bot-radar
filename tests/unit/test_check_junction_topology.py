"""Contrôle de jonction, sur des sorties de commande doublées.

Quatre issues doivent rester distinguables, et c'est l'objet de ces tests :
conforme, en écart, relevé impossible, droits insuffisants. Les confondre
conduirait soit à ajouter une règle déjà présente, soit à croire une jonction
saine sur un relevé qui n'a pas eu lieu.
"""

from __future__ import annotations

import pytest

from scripts.check_junction_topology import (
    EXIT_MISMATCH,
    EXIT_OK,
    EXIT_PRIVILEGE,
    EXIT_UNKNOWN,
    PrivilegeError,
    ProbeError,
    check,
    firewall_rule_present,
    main,
    read_key_permitlisten,
    read_network,
    read_permitlisten,
    verify,
)

SUBNET = "172.30.0.0/16"
GATEWAY = "172.30.0.1"
AUTRE_GATEWAY = "172.31.0.1"
COMPTE = "compte-tunnel"
CLE_CONFORME = (
    "restrict,port-forwarding,"
    f'permitlisten="{GATEWAY}:1080",command="/bin/false" ssh-ed25519 AAAAfake'
)


def _runner(reponses: dict[str, tuple[int, str, str]]):
    def run(script: str) -> tuple[int, str, str]:
        for prefixe, reponse in reponses.items():
            if script.startswith(prefixe):
                return reponse
        raise AssertionError(f"script inattendu : {script}")

    return run


def _run_complet(
    permitlisten: str = GATEWAY,
    regle: tuple[int, str, str] = (0, "", ""),
    cle: str | None = CLE_CONFORME,
    subnet: str = SUBNET,
):
    """Répond pour le compte de test et pour le compte par défaut."""
    return _runner(
        {
            "docker network inspect": (0, f"{subnet} {GATEWAY}\n", ""),
            "sshd -T": (0, f"permitlisten {permitlisten}:1080\n", ""),
            "getent passwd": (0, "/home/compte-tunnel\n", ""),
            "cat ": (0, (cle or "") + "\n", ""),
            "iptables -C": regle,
        }
    )


def test_reseau_lu_en_deux_parties():
    assert read_network(_run_complet()) == (SUBNET, GATEWAY)


@pytest.mark.parametrize("sortie", ["", f"{SUBNET}\n", f"{SUBNET} {GATEWAY} extra\n"])
def test_reseau_incomplet_est_refuse(sortie):
    run = _runner({"docker network inspect": (0, sortie, "")})

    with pytest.raises(ProbeError):
        read_network(run)


def test_permitlisten_lu_sans_port():
    assert read_permitlisten(_run_complet(), COMPTE) == GATEWAY


def test_permitlisten_sur_un_autre_port_est_refuse():
    run = _runner({"sshd -T": (0, f"permitlisten {GATEWAY}:2222\n", "")})

    with pytest.raises(ProbeError, match="port"):
        read_permitlisten(run, COMPTE)


def test_permitlisten_calcule_sans_droits_leve_privilege():
    """Un `sshd -T` refusé est un problème de droits, pas une absence de règle."""
    run = _runner({"sshd -T": (255, "", "Permission denied")})

    with pytest.raises(PrivilegeError):
        read_permitlisten(run, COMPTE)


def test_cle_conforme_rend_son_permitlisten():
    assert read_key_permitlisten(_run_complet(), COMPTE) == f"{GATEWAY}:1080"


def test_cle_sans_permitlisten_rend_none():
    """Absence d'option : la clé n'est pas restreinte, ce n'est pas « conforme »."""
    run = _run_complet(cle="ssh-ed25519 AAAAfake")

    assert read_key_permitlisten(run, COMPTE) is None


def test_cle_illisible_leve_une_erreur():
    run = _runner(
        {
            "getent passwd": (0, "/home/compte-tunnel\n", ""),
            "cat ": (1, "", ""),
        }
    )

    with pytest.raises(ProbeError):
        read_key_permitlisten(run, COMPTE)


def test_regle_presente_quand_iptables_repond_zero():
    assert firewall_rule_present(_run_complet(), SUBNET, GATEWAY) is True


def test_regle_absente_quand_iptables_repond_un():
    """Code 1 = règle absente : c'est un écart de terrain, pas un incident."""
    run = _run_complet(regle=(1, "", ""))

    assert firewall_rule_present(run, SUBNET, GATEWAY) is False


@pytest.mark.parametrize(
    "erreur",
    ["Permission denied (you must be root)", "iptables: Operation not permitted"],
)
def test_droits_insuffisants_ne_sont_pas_une_regle_absente(erreur: str):
    """Un refus de droits ne doit jamais être lu comme « règle manquante ».

    La confusion pousserait un opérateur à insérer une règle déjà présente, et
    à conclure « corrigé » sur un relevé qui n'a pas eu lieu.
    """
    run = _run_complet(regle=(4, "", erreur))

    with pytest.raises(PrivilegeError):
        firewall_rule_present(run, SUBNET, GATEWAY)


def test_un_autre_code_est_un_releve_impossible():
    """Ni présent, ni absent : le relevé n'a pas abouti."""
    run = _run_complet(regle=(127, "", "iptables: command not found"))

    with pytest.raises(ProbeError):
        firewall_rule_present(run, SUBNET, GATEWAY)


def test_jonction_conforme_quand_les_trois_concordent():
    assert check(GATEWAY, GATEWAY, True, f"{GATEWAY}:1080") == []


def test_la_configuration_sshd_sur_une_ancienne_adresse_est_un_ecart():
    """Gateway et clé à jour, configuration sshd restée sur l'ancienne adresse."""
    ecarts = check(GATEWAY, AUTRE_GATEWAY, True, f"{GATEWAY}:1080")

    assert len(ecarts) == 1
    assert "sshd" in ecarts[0]


def test_regle_absente_est_un_ecart():
    ecarts = check(GATEWAY, GATEWAY, False, f"{GATEWAY}:1080")

    assert len(ecarts) == 1
    assert "pare-feu" in ecarts[0]


def test_cle_non_conforme_est_un_ecart():
    """La clé peut viser une autre adresse que sshd : les deux sont comparées."""
    ecarts = check(GATEWAY, GATEWAY, True, f"{AUTRE_GATEWAY}:1080")

    assert len(ecarts) == 1
    assert "clé" in ecarts[0]


def test_les_trois_ecarts_sont_rendus_ensemble():
    """Gateway réelle, sshd et clé visant l'ancienne, règle absente : trois écarts."""
    ecarts = check(GATEWAY, AUTRE_GATEWAY, False, f"{AUTRE_GATEWAY}:1080")

    assert len(ecarts) == 3


def test_verify_conclut_au_conforme():
    assert verify(_run_complet(), COMPTE) == (True, [], False)


def test_verify_conclut_a_l_ecart():
    conforme, ecarts, droits = verify(
        _run_complet(permitlisten=AUTRE_GATEWAY), COMPTE
    )

    assert conforme is False
    assert ecarts
    assert droits is False


def test_verify_ne_conclut_pas_si_le_releve_echoue():
    conforme, _, droits = verify(
        _runner({"docker network inspect": (1, "", "docker indisponible")}), COMPTE
    )

    assert conforme is None
    assert droits is False


def test_verify_signale_les_droits_insuffisants():
    conforme, ecarts, droits = verify(
        _run_complet(regle=(4, "", "Permission denied")), COMPTE
    )

    assert conforme is None
    assert droits is True
    assert ecarts


def test_compte_lu_depuis_l_argument_jamais_en_dur():
    """Le compte est un paramètre : aucun nom n'est figé dans la logique."""
    vus: list[str] = []

    def run(script: str) -> tuple[int, str, str]:
        vus.append(script)
        if script.startswith("docker network inspect"):
            return 0, f"{SUBNET} {GATEWAY}\n", ""
        if script.startswith("sshd -T"):
            return 0, f"permitlisten {GATEWAY}:1080\n", ""
        if script.startswith("getent passwd"):
            return 0, "/home/autre\n", ""
        if script.startswith("cat "):
            return 0, f'permitlisten="{GATEWAY}:1080" ssh-ed25519 AAAA\n', ""
        return 0, "", ""

    verify(run, "autre")

    assert any("getent passwd autre" in s for s in vus)
    assert any("user=autre," in s for s in vus)


def test_la_sortie_ne_contient_ni_cle_ni_empreinte(monkeypatch, capsys):
    """Seule l'option est rendue : la clé elle-même ne peut pas fuir."""
    monkeypatch.setattr(
        "scripts.check_junction_topology.default_runner", _run_complet()
    )

    main(["--tunnel-user", COMPTE])

    sortie = capsys.readouterr().out
    assert "AAAAfake" not in sortie
    assert "ssh-ed25519" not in sortie


def test_main_rend_le_code_de_l_ecart(monkeypatch, capsys):
    monkeypatch.setattr(
        "scripts.check_junction_topology.default_runner",
        _run_complet(permitlisten=AUTRE_GATEWAY),
    )

    assert main([]) == EXIT_MISMATCH
    assert "EN ÉCART" in capsys.readouterr().out


def test_main_rend_zero_quand_tout_concorde(monkeypatch):
    monkeypatch.setattr(
        "scripts.check_junction_topology.default_runner", _run_complet()
    )

    assert main([]) == EXIT_OK


def test_main_rend_le_code_des_droits(monkeypatch, capsys):
    monkeypatch.setattr(
        "scripts.check_junction_topology.default_runner",
        _run_complet(regle=(4, "", "Permission denied")),
    )

    assert main([]) == EXIT_PRIVILEGE
    assert "droits insuffisants" in capsys.readouterr().out


def test_main_rend_le_code_du_releve_impossible(monkeypatch):
    monkeypatch.setattr(
        "scripts.check_junction_topology.default_runner",
        _runner({"docker network inspect": (1, "", "docker indisponible")}),
    )

    assert main([]) == EXIT_UNKNOWN


def test_les_quatre_codes_de_sortie_sont_distincts():
    assert len({EXIT_OK, EXIT_MISMATCH, EXIT_UNKNOWN, EXIT_PRIVILEGE}) == 4


def test_le_compte_peut_venir_de_lenvironnement(monkeypatch):
    """`TUNNEL_USER` évite de répéter le compte sur la ligne de commande."""
    monkeypatch.setenv("TUNNEL_USER", "compte-env")
    monkeypatch.setattr(
        "scripts.check_junction_topology.default_runner", _run_complet()
    )

    assert main([]) == EXIT_OK


def test_sans_compte_le_controle_reste_possible_mais_partiell(monkeypatch, capsys):
    """Sans compte, gateway et règle INPUT sont contrôlés ; le reste ne l'est pas.

    Le compte n'est ni choisi ni deviné : un nom par défaut figé dans le script
    ferait croire à un contrôle complet sur une installation qui ne le prévoit pas.
    """
    monkeypatch.delenv("TUNNEL_USER", raising=False)
    monkeypatch.setattr(
        "scripts.check_junction_topology.default_runner", _run_complet()
    )

    code = main([])

    err = capsys.readouterr().err
    assert "compte de tunnel non fourni" in err
    assert code == EXIT_OK
