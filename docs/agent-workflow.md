# Travail des agents, permissions et reprise

Statut : procédure actuelle. Périmètre : développement local, découverte, livraison
et passation. [AGENTS](../AGENTS.md) fixe les invariants et la définition de terminé.

## Mandat et espaces

Définir avant modification les critères observables et moyens de preuve autorisés.
Le plan validé reste la référence ; distinguer exigence produit, lecture du code,
hypothèse, test synthétique et observation réelle. Corriger puis revérifier jusqu'au
succès ou à une limite réelle du mandat, sans objectif de durée ou de consommation.

Le dossier `/Users/aminezegmou/Projets/bot-radar` reste la référence, généralement sur
`main`. Avant un worktree : status, branche, SHA, index, autres worktrees,
sous-module ; chemin et branche doivent être libres. Avec permission réseau,
rafraîchir `origin/main`, comparer et fixer la base ; sinon la déclarer locale.
Ne jamais déplacer, stash ou récupérer les travaux d'une autre tâche.

Chaque agent travaille dans le chemin absolu de son espace, avec mandat explicite.
Coordonner les opérations administratives Git/sous-module partagées. Le nettoyage
nécessite permission explicite et contrôle préalable : contenu, fichiers ignorés
utiles, commits non livrés, utilité de l'espace et agents actifs. Aucun prune automatique.
Sur support amovible : montage stable, dépôt principal accessible, pas de déconnexion.
`git worktree lock` protège les métadonnées contre l'élagage, pas contre les écritures.

Un worktree n'isole ni ports, DB, Redis, conteneurs, volumes ni environnement Python.
Si installation autorisée : venv propre, verrou existant, pas d'editable partagé
pointant sur une autre tâche. Sans installation, vérifier la provenance des imports
lors de la réutilisation d'un interpréteur existant. PostgreSQL exige une base
jetable par vérification : les fixtures peuvent recréer le schéma. Compose a des
noms/ports fixes ; `-p` ne suffit pas. Aucun service non mandaté.

## Intégration de Superpowers

La [politique par tâche](../AGENTS.md#superpowers-selon-la-tâche) est canonique dans
AGENTS : ses exemptions bornent le protocole tiers, elles ne modifient pas les skills.
Ne pas recopier ici les méthodes de conception, débogage, TDD, planification ou revue.
Ne lire que la skill pertinente et l'adaptation utile au harness réellement utilisé.

La source et le commit sont ceux de [.gitmodules](../.gitmodules) et du gitlink
`vendor/superpowers`. Vérifier `git ls-tree HEAD vendor/superpowers` et le SHA du
checkout avant lecture. Aucun upgrade automatique ; une évolution exige un commit
choisi et l'autorisation correspondante. Ne pas copier ou modifier le contenu tiers.

### Compatibilité et capacités

Le protocole [using-superpowers](../vendor/superpowers/skills/using-superpowers/SKILL.md)
référence des adaptations Claude Code, Codex, Pi, Antigravity, Hermes et Muse ; la
version épinglée contient aussi `gemini-tools.md`. Leur présence ne prouve pas leur
compatibilité avec les outils ou versions de la session. Les références Codex et
Antigravity sont dans `using-superpowers/references/` ; les consulter au besoin,
sans importer leurs exemples de configuration personnelle comme des permissions.

Utiliser les principes compatibles et les adaptations disponibles. Sans adaptation,
repérer les capacités requises, exploiter celles réellement disponibles et annoncer
la limite ; ne pas promettre l'application intégrale d'une skill « telle quelle ».
Un sous-agent inexistant ne peut pas être invoqué ; une auto-revue doit être nommée
comme telle. Si une revue indépendante est requise et indisponible, la signaler
manquante. Un worktree n'est pas une sandbox et les instructions ne font pas respecter
les permissions. Les opérations Git, installations et nettoyages proposés par une
skill restent soumis au mandat, comme les autres opérations.

### Découverte dans le dépôt

| Entrée configurée | Rôle vérifiable dans les fichiers |
| --- | --- |
| `AGENTS.md` racine | Source commune ; chargement dépendant du harness |
| `.cursor/skills/*` | Liens de découverte vers les skills du sous-module |
| `.agent/skills/*`, `GEMINI.md` | Liens de skills et import minimal de la source commune |

Ne pas ajouter d'adaptateur pour un outil non configuré ni modifier les paramètres
globaux. Cette table ne certifie pas un chargement en session.
Distinguer dans les preuves : configuration présente, cibles résolues/accessibles,
découverte effective par l'outil, invocation observée, puis workflow réellement
exécuté. Une vérification de symlink prouve seulement les deux premiers niveaux si
sa cible est accessible, pas les trois suivants.

Un sous-module non initialisé laisse les cibles indisponibles dans ce worktree :
le signaler, ne pas télécharger sans permission. Une copie locale déjà accessible
peut être lue si autorisée, après vérification du SHA ; cela ne répare pas la
découverte dans le worktree. Ne pas remplacer les liens par des copies de skills.
Les installations personnelles/globales et leurs doublons restent non vérifiés
lorsqu'ils sortent du mandat ; ne pas revendiquer leur absence.

## Scripts sensibles

Sans demande explicite, ne modifier ni altérer les scripts suivants :
`sync_vinted_session.py`, `sync_vinted_checkout_session.py`, `sync_chasse_session.py`,
`refresh_chasse_token.py`, `refresh_chasse_cron.sh`, `local_vinted_poller.py`,
`report_cote_sent_state.py`, `audit_alerts_identity.py`, `backfill_series_tomes.py`,
`clean_alert_keywords.py` (tous dans `scripts/`). Cette liste conserve la protection
historique des utilitaires, sans autoriser leur exécution.

Un diagnostic en lecture seule peut consulter réseau/Redis/DB : « lecture seule »
ne signifie pas « hors réseau ». Le dry-run du backfill consulte le catalogue.
Le backfill réutilise le chemin de liaison central, ne calcule pas son propre total
et attend ses cotations avant de sortir. Le nettoyeur réutilise les motifs du domaine :
`--apply` exige un diff lu sauf dérogation `--yes` explicitement autorisée ;
`--only-degenerate` exclut les retraits dépendant du titre, déduplication ou plafond.
Les rapports d'identité distinguent panne, repli et confirmation ; leur population
et leur date doivent rester visibles. Un compteur d'envoi nul ne prouve rien sans
mesure préalable. `python-dotenv` sert au poller local, pas au runtime Docker ;
les scripts ne sont importés dans `app/` qu'à dessein.

## Vérifications sans secrets ni services

Lire une recette avant de l'exécuter. Neutraliser les `.env` réels et les répertoires
de secrets **avant** les imports ; utiliser uniquement des valeurs factices et
refuser/doubler les transports externes, y compris dans les enfants. Ne pas lancer
l'application pour vérifier du texte : son lifespan configure Telegram et les jobs.

[verify_consolidation.py](../scripts/verify_consolidation.py) propose les modes
`unit` et `typecheck` avec environnement nettoyé et gardes de transports. `unit`
sans cible sélectionne unitaires et performance ; `--target` peut sélectionner
explicitement des tests d'intégration compatibles SQLite. `make test-isolated`
sélectionne `unit --target tests` via cette même recette, sans second dispositif.
Les modes `postgres`
et `backup` créent des ressources et nécessitent un mandat distinct.
Les contrôles de Python et de ses enfants ne sont pas une sandbox système générale.
`make verify` contacte GitHub ; `make check-history` crée des worktrees et hérite
de l'environnement. Aucun de ces noms ne vaut autorisation.

Pour le code : Ruff ciblé puis tests concernés ; validation complète finale selon
AGENTS. Pour la documentation, distinguer prose, instructions et procédures sensibles.
Benchmarks sans autre vérification concurrente. Ne jamais changer une attente pour
masquer un échec. Attribuer toute coroutine non attendue à un double après reproduction,
pas après simple lecture ; voir [les limites C13](open-issues.md).

## Sécurité et preuves existantes

Les quatre tokens d’accès obligatoires sont `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_WEBHOOK_SECRET`, `INGEST_API_TOKEN`, `INTERNAL_API_TOKEN`.
La validation production refuse une valeur vide avant la configuration Telegram
et le démarrage des producteurs. Préserver les masquages Bearer, paires token/secret/password/key,
préfixe `sk-` et extras sensibles, y compris les exceptions et structures imbriquées.
Les contrôles [de rédaction](../tests/unit/test_logging_redaction.py) et
[de secrets](../tests/unit/test_utils_and_core.py) couvrent leurs cas synthétiques.
[Le webhook](../tests/integration/test_telegram_webhook_transient_failure.py) et
[le cycle de vie](../tests/unit/test_unified_lifespan.py) couvrent des frontières
supplémentaires ; ils n’attestent ni configuration ni transport de production.
Un garde-fou critique absent doit faire l’objet d’un lot séparé, pas d’un hook,
superviseur ou élargissement de CI ajouté pendant une réorganisation documentaire.

## Livraison Git lorsqu'elle est autorisée

Branche depuis main, index explicite et relu. Les contrôles ciblés des commits
intermédiaires et la validation finale suivent la
[politique de vérification](../AGENTS.md#vérification-et-définition-de-terminé).
`make pr MESSAGE=… TITLE=… BRANCH=… STAGE=…`
crée commit, push et PR via `gh pr create` : ne pas l'exécuter sans ces permissions.
Pour un texte multiligne, utiliser `MESSAGE_FILE`. La PR ne contient que cette tâche.

Avant merge : contrôles locaux requis satisfaits, CI `check` et `test-postgres`
vertes sur la PR et branche à jour. Réutiliser les preuves locales inchangées selon
AGENTS ; `make verify` attend seulement la CI, sans relancer lint ni tests.
L'auto-revue, la confirmation unique liée au commit présenté et l'arrêt en cas
d'approbation formelle GitHub manquante suivent la
[convention de travail](../AGENTS.md#convention-de-travail). L'accord de merge
n'atteste aucune revue humaine et n'autorise aucune opération sensible supplémentaire.
La protection documentée exige `check`, `strict=true`, `enforce_admins` : ne jamais
la lever pour livrer. Renommer ce job exige de mettre à jour ses contexts dans un
mandat administratif distinct. Une lecture locale ne vérifie pas l'état GitHub actuel.

## Passation uniquement si une reprise est nécessaire

Un message ou une note temporaire suffit ; pas de mémoire cumulative obligatoire.

```text
Objectif / plan validé / critères :
Permissions et interdictions :
Worktree absolu / branche / SHA de base et courant :
Diff et index / travaux préexistants à préserver :
Preuves datées : commande, résultat, arbre et portée :
Travaux restants / ressources temporaires encore actives :
Limites / blocages / prochaine étape autorisée :
```

À la reprise, relire l'état réel avant d'agir. Une preuve antérieure ne valide pas
un arbre changé. Un plafond documentaire ne prouve ni arrêt des sous-processus ni
possibilité d'exécution nocturne sans surveillance.
