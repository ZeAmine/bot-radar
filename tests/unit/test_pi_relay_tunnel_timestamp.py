"""Le gabarit de tunnel date chaque lancement avant d'ouvrir la connexion."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

GABARIT = Path(__file__).resolve().parents[2] / "deploy" / "pi-relay" / "run_tunnel.sh"

HORODATAGE_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z tunnel_start$")


def _environnement_isole(tmp_path: Path) -> dict[str, str]:
    """Environnement sans valeur héritée, plus les seuls paramètres requis."""
    (tmp_path / "logs").mkdir()
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RELAY_DIR": str(tmp_path),
        "TUNNEL_KEY": str(tmp_path / "cle"),
        "TUNNEL_REMOTE": "compte@hote.invalide",
        "TUNNEL_LISTEN_ADDRESS": "172.0.0.1",
    }


def test_le_gabarit_ecrit_un_horodatage_utc(tmp_path: Path) -> None:
    """`ssh` n'horodate pas sa sortie : la ligne doit précéder l'`exec`.

    Seule la ligne d'horodatage est exécutée : `exec` remplacerait le shell et
    ouvrirait une connexion réelle, hors du périmètre d'un test unitaire.
    """
    environnement = _environnement_isole(tmp_path)
    ligne = next(
        ligne
        for ligne in GABARIT.read_text(encoding="utf-8").splitlines()
        if "tunnel_start" in ligne and "printf" in ligne
    )

    subprocess.run(
        ["/bin/sh", "-c", ligne],
        env=environnement,
        capture_output=True,
        text=True,
        check=True,
    )

    contenu = (tmp_path / "logs" / "tunnel.log").read_text(encoding="utf-8").strip()
    assert HORODATAGE_UTC.match(contenu), f"format inattendu : {contenu!r}"


def test_l_horodatage_precede_l_exec() -> None:
    """L'ordre compte : `exec` remplace le shell, donc plus rien ne s'écrit après."""
    lignes = GABARIT.read_text(encoding="utf-8").splitlines()
    index_horodatage = next(i for i, ligne in enumerate(lignes) if "tunnel_start" in ligne)
    index_exec = next(
        i for i, ligne in enumerate(lignes) if ligne.strip().startswith("exec ")
    )
    assert index_horodatage < index_exec
    assert index_exec - index_horodatage == 2, "une ligne vide sépare l'horodatage de l'exec"


def test_le_gabarit_reste_en_posix_sh() -> None:
    """`cron` exécute avec `/bin/sh` : le script ne doit pas supposer bash."""
    lignes = GABARIT.read_text(encoding="utf-8").splitlines()
    assert lignes[0] == "#!/bin/sh"
    source = "\n".join(lignes)
    assert "[[ " not in source
    assert "function " not in source


def test_l_horodatage_reste_utc_malgre_un_fuseau_local(tmp_path: Path) -> None:
    """Un fuseau local non UTC ne doit pas décaler l'horodatage.

    Sans `date -u`, la ligne porterait l'heure locale tout en se terminant par
    `Z` : elle serait fausse sans en avoir l'air.
    """
    environnement = _environnement_isole(tmp_path)
    environnement["TZ"] = "Asia/Tokyo"
    ligne = next(
        ligne
        for ligne in GABARIT.read_text(encoding="utf-8").splitlines()
        if "tunnel_start" in ligne and "printf" in ligne
    )

    subprocess.run(
        ["/bin/sh", "-c", ligne],
        env=environnement,
        capture_output=True,
        text=True,
        check=True,
    )

    contenu = (tmp_path / "logs" / "tunnel.log").read_text(encoding="utf-8").strip()
    ecrit = contenu.split(" ")[0]
    reference = subprocess.run(
        ["/bin/sh", "-c", "date -u +%Y-%m-%dT%H:%M:%SZ"],
        env=environnement,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    heures_ecart = abs(int(ecrit[11:13]) - int(reference[11:13])) % 24
    assert heures_ecart == 0, f"horodatage local au lieu d'UTC : {ecrit} vs {reference}"
