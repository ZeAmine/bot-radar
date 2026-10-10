# Autonomie bornée (lot 1)

Ce lot permet de valider une fiche une fois, puis de laisser l'agent travailler dans un worktree dédié jusqu'à un rapport **prêt à revoir** ou **arrêté**, sans réinventer la CI.

## Fichiers

| Fichier | Rôle |
| --- | --- |
| [scripts/task_verdict.py](../scripts/task_verdict.py) | Fiche, approbation, worktree, `verify`, état, rapport |
| [scripts/task_guard.py](../scripts/task_guard.py) | Entrée des hooks Cursor (décisions déterministes) |
| [docs/autonomy/task.example.toml](autonomy/task.example.toml) | Gabarit de fiche |

État, approbations et rapports vivent hors dépôt : `~/.cursor/projects/Users-aminezegmou-Projets-bot-radar/autonomy/<id>/`.

Worktrees de tâche : `~/Projets/bot-radar-tasks/<id>`.

Variables optionnelles : `BOTRADAR_AUTONOMY_HOME`, `BOTRADAR_TASKS_ROOT`.

## Déroulé humain

1. Copier le gabarit vers un chemin hors dépôt et le remplir (`base_sha`, `allowed_write`, `proof`).
2. **Approbation** (humain uniquement) depuis le checkout de confiance :
   `python scripts/task_verdict.py approve /chemin/task.toml`
   Enregistre l'empreinte SHA-256 de la fiche et les empreintes des scripts de confiance.
3. **Démarrage** :
   `python scripts/task_verdict.py start /chemin/task.toml`
   Crée le worktree détaché et l'état (compteurs à zéro).
4. Lancer l'agent dans le worktree. Les hooks utilisateur (si installés) appliquent [scripts/task_guard.py](../scripts/task_guard.py).
5. Boucle agent : modifier → `python scripts/task_verdict.py verify <id>` jusqu'à vert ou arrêt.
6. **Rapport** :
   `python scripts/task_verdict.py report <id> [--consumption-after "..."]`

**Reprise** sans remise à zéro des plafonds :

`python scripts/task_verdict.py resume <id>`

L'agent ne doit pas exécuter `approve`, `start`, `resume` ni modifier les scripts de confiance.

## Critères de `verify`

- Cohérence fiche / approbation / `active.json`
- `HEAD` descendant du `base_sha`
- Fichiers touchés (y compris non suivis, suppressions, renommages) dans `allowed_write`, hors `forbidden`
- Pas d'affaiblissement des tests existants (assertions, `skip`, `xfail`, `# noqa`, `# type: ignore`)
- Nombre de tests collectés ≥ baseline au démarrage
- Taille du diff ≤ `max_diff_lines`
- Pas de motif de secret dans le diff
- Scripts de confiance inchangés depuis l'approbation
- Plafonds : durée, cycles `verify`, appels d'outils
- Commandes `proof` (liste blanche : `pytest …`, `ruff check …`, `mypy app`)

## Hooks Cursor (installation manuelle, hors dépôt)

Ne pas versionner `~/.cursor/hooks.json` depuis ce dépôt. Exemple à adapter avec le **chemin absolu** du checkout de confiance :

```json
{
  "version": 1,
  "hooks": {
    "beforeShellExecution": [
      {
        "command": "/Users/aminezegmou/Projets/bot-radar/scripts/task_guard.py",
        "failClosed": true
      }
    ],
    "preToolUse": [
      {
        "command": "/Users/aminezegmou/Projets/bot-radar/scripts/task_guard.py",
        "failClosed": true
      }
    ],
    "subagentStart": [
      {
        "command": "/Users/aminezegmou/Projets/bot-radar/scripts/task_guard.py",
        "failClosed": true
      }
    ]
  }
}
```

Rendre le script exécutable : `chmod +x scripts/task_guard.py`.

Sans tâche active (`autonomy/active.json` absent), le garde laisse passer : les autres sessions Cursor ne sont pas bloquées.

### Essai réel des hooks (non exécuté automatiquement)

**Objectif** : prouver que le garde refuse des commandes inoffensives avant de faire confiance à l'autonomie.

**Prérequis** : fiche approuvée et démarrée, hooks installés, `failClosed: true`.

**Procédure** (session Agent dans le worktree de la tâche) :

1. Tenter `ssh -V` → **attendu** : refus explicite (permission deny).
2. Tenter `rsync --version` → **attendu** : refus.
3. Tenter `docker --version` → **attendu** : refus.
4. Tenter `python scripts/task_verdict.py approve …` → **attendu** : refus (réservé humain).
5. Tenter `pytest tests/unit/…` autorisé par la fiche → **attendu** : allow (si chemin dans la preuve).

**Critères d'échec du lot côté hooks** : l'une des commandes 1–4 passe, ou `approve` passe depuis l'agent.

**Si les hooks ne se déclenchent pas** : l'outillage reste utile en mode supervisé ; documenter la version de Cursor et l'onglet Hooks.

## Limites assumées

| Sujet | Limite |
| --- | --- |
| Réseau | `verify` lance `pytest` / outils locaux ; seul Telegram réel est bloqué dans `tests/conftest.py`. Pas de preuve d'absence d'autres appels sortants. |
| Sécurité | Liste blanche shell et périmètre d'écriture ; pas de sandbox du code exécuté **dans** un test écrit par l'agent. |
| Arrêt | Effectif sur outils hookés ; pas sur une réponse chat seule. Arrêt manuel toujours possible. |
| Consommation | Non lue par les scripts ; relevé humain ou « non mesurée » dans le rapport. Plafonds = durée, cycles, appels d'outils. |
| Hooks | Comportement réel dépend de Cursor ; les tests du dépôt couvrent le schéma JSON et la logique `decide`, pas l'IDE. |

## Commandes utiles

```bash
python scripts/task_verdict.py status <id>
python scripts/task_verdict.py note <id> "message"
```

Validation locale du lot (scripts) :

```bash
pytest tests/unit/test_task_verdict.py tests/unit/test_task_guard.py -v
ruff check scripts/task_verdict.py scripts/task_guard.py tests/unit/test_task_verdict.py tests/unit/test_task_guard.py
mypy scripts/task_verdict.py scripts/task_guard.py
```
