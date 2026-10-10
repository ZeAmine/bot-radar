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

Les contrôles statiques s'exécutent avant toute collecte pytest et avant les preuves. Un échec statique n'exécute pas le code des tests.

- Cohérence fiche / approbation / `active.json`
- `HEAD` descendant du `base_sha`
- Fichiers touchés dans `allowed_write`, hors `forbidden` : suivis, non suivis, suppressions, et les deux chemins d'un renommage ou d'une copie
- Lien symbolique non suivi qui sort du worktree
- Modification d'un test déjà présent au `base_sha` : arrêt immédiat pour revue humaine
- Pas d'affaiblissement d'un nouveau test (assertions, `skip`, `xfail`, `# noqa`, `# type: ignore`)
- Nombre de tests collectés ≥ baseline au démarrage, seulement si les contrôles statiques passent
- Taille du diff ≤ `max_diff_lines`, non suivis compris ; un binaire n'est pas compté comme zéro ligne
- Motif de secret dans le diff, dans un fichier non suivi ou dans son nom
- Scripts de confiance inchangés depuis l'approbation
- Plafonds : durée, cycles `verify` (compteur persisté avant les preuves), appels d'outils. Une violation de contrôle arrête dès la première vérification. Le temps restant est recalculé avant chaque sous-processus ; aucun nouveau lancement n'a lieu après l'échéance.
- Le digest est relevé avant et après les preuves. S'il change, ou si un arrêt concurrent est apparu, le résultat n'est pas publié comme vert.
- Commandes `proof` : argv exact, exécutées seulement par les binaires du venv de confiance. Le shell de la session liée n'accepte que ces mêmes binaires et ces mêmes commandes. Un nom nu, ou un pytest absent de la fiche, est refusé. L'environnement transmis ne reprend pas les variables du processus parent.

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

Le garde lit le schéma documenté par Cursor : `hook_event_name`, `conversation_id`, `workspace_roots`, et pour `preToolUse` les champs `tool_name` et `tool_input`. Les événements acceptés sont `beforeShellExecution`, `preToolUse` et `subagentStart`. Les outils acceptés sont `Shell`, `Read`, `Write`, `Delete`, `Grep` et `Task`. Un outil `MCP:*` est refusé. Les alias inventés (`event`, `toolName`, `input`) ne sont pas lus.

La session est liée par `conversation_id` au premier appel dont `workspace_roots` contient le worktree. L'identité ne dépend pas du répertoire courant : un `cwd` ou un `working_directory` hors worktree est refusé. Une autre conversation ouverte sur le même worktree est refusée. Une session dont les racines ne contiennent pas ce worktree n'est pas bloquée.

Dans la session liée, le défaut est le refus : événement inconnu, outil inconnu, champ manquant, fiche ou approbation incohérente. Une conversation déjà liée est reconnue avant toute décision fondée sur les racines : des racines absentes ou différentes refusent la session, au lieu de la traiter comme une autre session. Le garde relit la fiche, l'approbation et l'état avant l'action. La réservation d'un appel est atomique et relit le statut sous le verrou : `stopped` ou `failed` n'autorise aucune nouvelle modification, ne remet pas les compteurs à zéro et ne reprend pas la tâche. Un même `tool_use_id` ne compte qu'une fois, et `beforeShellExecution` du même appel shell reste autorisé lorsque `preToolUse` vient de prendre la dernière place, tant que le statut est encore `active`. Les lectures passent par le chemin résolu : secret, métadonnées `.git` et lien sortant sont refusés, y compris pour `Grep` et `rg`.

En `ready_for_review`, les écritures et les commandes de modification sont refusées. `verify` ou `report` recalcule le digest ; s'il a changé, le statut redevient `active`, le digest est effacé, et un nouveau `verify` est exigé. Les compteurs ne sont pas remis à zéro. `resume` (humain) invalide aussi cet état, sans remettre les compteurs à zéro. Un arrêt `stopped` ou `failed` n'est pas repris par `report`.

`failClosed: true` ne couvre qu'un crash, un timeout ou une sortie invalide du hook. Il ne corrige pas une décision `allow` incorrecte.

Sans tâche active (`autonomy/active.json` absent), le garde laisse passer : les autres sessions Cursor ne sont pas bloquées.

### Essai réel des hooks (non exécuté automatiquement)

**Objectif** : prouver que le garde refuse des commandes inoffensives avant de faire confiance à l'autonomie.

**Prérequis** : fiche approuvée et démarrée, hooks installés, `failClosed: true`.

**Procédure** (session Agent dans le worktree de la tâche) :

1. Tenter `ssh -V` → **attendu** : refus explicite (permission deny).
2. Tenter `rsync --version` → **attendu** : refus.
3. Tenter `docker --version` → **attendu** : refus.
4. Tenter `python scripts/task_verdict.py approve …` → **attendu** : refus (réservé humain).
5. Tenter le binaire `.venv/bin/pytest` d'une commande présente dans `proof` → **attendu** : allow. Un pytest absent de la fiche, ou un exécutable hors de ce venv, est refusé.

**Critères d'échec du lot côté hooks** : l'une des commandes 1–4 passe, ou `approve` passe depuis l'agent.

**Si les hooks ne se déclenchent pas** : l'outillage reste utile en mode supervisé ; documenter la version de Cursor et l'onglet Hooks.

## Limites assumées

| Sujet | Limite |
| --- | --- |
| Réseau | `verify` lance `pytest` / outils locaux avec un environnement explicite et les jetons factices de test. Aucune garantie réseau supplémentaire n'est ajoutée. |
| Sécurité | Liste blanche exacte des argv et périmètre d'écriture. Pas de sandbox du code pytest. |
| Arrêt | Effectif sur outils hookés ; pas sur une réponse chat seule. Arrêt manuel toujours possible. Les sous-processus de `verify` ont un timeout. |
| Consommation | Non lue par les scripts ; relevé humain ou « non mesurée » dans le rapport. Plafonds = durée, cycles, appels d'outils. |
| Hooks | Comportement réel dépend de Cursor ; les tests du dépôt couvrent le schéma JSON documenté et la logique de décision, pas l'IDE. `failClosed` ne compense pas un `allow` incorrect. |
| Rapport | Les preuves affichées sont celles enregistrées par `verify` (commande, code, durée, fin de sortie). Un digest différent invalide `ready_for_review` et marque la preuve périmée, sans relancer les commandes ni remettre les compteurs à zéro. |

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
