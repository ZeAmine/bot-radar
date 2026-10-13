# Ancien garde d’autonomie — dispositif retiré

Le dispositif `task_verdict` / `task_guard`, son gabarit et ses tests dédiés ont
été retirés par le mandat de cohérence du 13 octobre 2026. Aucun hook, orchestrateur
ou script de relance ne les remplace. La règle commune reste dans
[AGENTS.md](../AGENTS.md#pilotage-autonome-borné) et les skills existants.

Avant d’appliquer ce retrait à une autre installation, vérifier les hooks Cursor
qui appelleraient ces scripts et faire terminer ou suspendre explicitement leurs
sessions. Leur désactivation exige une permission ; la suppression du dépôt ne
prouve pas qu’une configuration personnelle ait été désactivée.

Les fiches, approbations, journaux et rapports hors dépôt, ainsi que les anciens
worktrees, restent à leur place. Ne pas les déplacer, supprimer ou réutiliser
automatiquement. La convention de travail ne nécessite aucun de ces anciens états.
