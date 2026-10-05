"""Les identifiants de révision Alembic tiennent dans la colonne qui les porte.

Alembic crée `alembic_version.version_num` en `VARCHAR(32)`. Un identifiant plus
long n'échoue ni à la lecture du graphe, ni au `create_index`, ni au `downgrade` :
il échoue au moment précis où Alembic écrit la nouvelle version, après avoir déjà
exécuté le DDL de la migration. En production cela veut dire des index créés puis
la migration annulée, et un schéma en retard sur le code sans que rien le signale.

C'est exactement ce qu'a produit `0006_composite_indexes_for_alert_and_cache_queries`,
50 caractères. La CI ne l'a pas vu parce que le job `test-postgres` joue
`pytest tests/integration/` et jamais `alembic upgrade head` : la chaîne de
migration n'y est pas exercée. Ce test ferme l'angle sans base de données, et
échoue à la rédaction plutôt qu'au déploiement.

`down_revision` est mesuré pour la même raison : un ancêtre trop long doit être
écrit dans la même colonne dès qu'il devient une tête.
"""

import ast
from pathlib import Path

import pytest

VERSIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

# La largeur créée par Alembic pour `alembic_version.version_num`.
VARCHAR_32 = 32


def _migration_files() -> list[Path]:
    files = sorted(VERSIONS_DIR.glob("*.py"))
    assert files, "aucune migration trouvée : le chemin des versions a-t-il changé ?"
    return files


def _revision_ids(path: Path) -> tuple[str, str | None]:
    """Relit `revision` et `down_revision` sans exécuter le module.

    Importer la migration déclencherait ses imports ; `ast` suffit à lire
    ces deux annotations, qui sont des constantes.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    valeurs: dict[str, str | None] = {}
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id in {"revision", "down_revision"} and isinstance(
            node.value, ast.Constant
        ):
            valeurs[node.target.id] = node.value.value
    assert "revision" in valeurs, f"{path.name} ne déclare pas `revision`"
    return valeurs["revision"], valeurs.get("down_revision")


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.name)
def test_revision_id_tient_dans_la_colonne(path: Path) -> None:
    """Chaque identifiant écrit dans `version_num` tient dans 32 caractères."""
    revision, down_revision = _revision_ids(path)

    trop_long = [i for i in (revision, down_revision) if i and len(i) > VARCHAR_32]
    assert not trop_long, (
        f"{path.name} : identifiant de plus de {VARCHAR_32} caractères ("
        + ", ".join(f"{i!r} ({len(i)})" for i in trop_long)
        + "). Alembic écrira ce texte dans `alembic_version.version_num`, un "
        "VARCHAR(32) : la migration échouera en fin d'exécution, après son DDL. "
        "Raccourcir l'identifiant."
    )


def test_une_seule_tete_de_migration() -> None:
    """Le graphe n'a qu'une tête, et les identifiants sont uniques.

    Deux têtes feraient `alembic upgrade head` échouer en production ; deux
    migrations de même identifiant feraient le graphe se confondre.
    """
    ids = [_revision_ids(p) for p in _migration_files()]
    revisions = [rev for rev, _ in ids]
    ancetres = {down for _, down in ids if down}

    assert len(revisions) == len(set(revisions)), "deux migrations portent le même identifiant"

    tetes = [rev for rev in revisions if rev not in ancetres]
    assert len(tetes) == 1, f"une seule tête attendue, obtenue : {tetes}"