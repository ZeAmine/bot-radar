# BotRadar — instructions communes

Statut : règles actives pour les assistants travaillant dans ce dépôt privé.
Dépôt : `git@github.com:ZeAmine/bot-radar.git`, branche de référence `main`.

BotRadar détecte des annonces Vinted, compare les BD au marché d'occasion livré
et alerte un utilisateur Telegram autorisé. L'IA conseille ; elle ne décide pas.

## Autorité et permissions

- Le mandat utilisateur borne chaque tâche : objectif, périmètre, permissions,
  interdictions et critères observables. Une procédure ne donne aucune permission.
- Sans permission d'écriture, rester en lecture seule. Dans le périmètre autorisé,
  analyser, modifier, contrôler, corriger et revérifier sans confirmations répétées.
- S'arrêter si une permission ou un arbitrage important manque, si un risque nouveau
  apparaît, si la vérification est indisponible, si l'investigation ne progresse plus
  ou si le mandat est atteint.
  Respecter aussi les arrêts d'investigation applicables des skills.
- Sous réserve des instructions applicables, le mandat et ces règles produit
  bornent les méthodes Superpowers, puis les défauts IDE. Une skill n'accorde
  aucune permission supplémentaire, y compris pour Git ou les outils globaux.
  Lire [using-superpowers](vendor/superpowers/skills/using-superpowers/SKILL.md)
  pour sélectionner les skills pertinentes, avec les exemptions locales ci-dessous.
- Ne pas remplacer ces instructions par celles de Superpowers, modifier les skills
  tiers ou actualiser automatiquement le sous-module. Son évolution exige un
  commit choisi. Ne pas installer Agent OS, ECC, BMad, GSD ou Spec Kit ici.
- Ne jamais confondre une consigne Markdown avec une sandbox, un superviseur ou
  une garantie d'autonomie sans surveillance. Un plafond écrit n'arrête aucun processus.

## Superpowers selon la tâche

Ces exemptions sont autorisées pour BotRadar ; elles priment sur les enchaînements
systématiques des skills tierces. Disponibilité ne signifie pas chargement permanent.

| Tâche | Application attendue |
| --- | --- |
| Lecture, explication, audit | Investigation et preuves adaptées ; pas de workflow d'implémentation |
| Correction documentaire mécanique | Édition ciblée, références et diff ; ni brainstorming, sous-agents ni plan séparé par défaut |
| Bug | Diagnostic structuré, reproduction si possible, preuve du correctif et de non-régression |
| Fonctionnalité spécifiée et autorisée | Exécuter mandat et critères ; ne rouvrir les choix validés qu'en cas de contradiction ou information importante nouvelle |
| Fonctionnalité ambiguë, architecture | Clarification et conception proportionnées avant implémentation |

Toute modification exige vérification adaptée et bilan honnête, sans abaisser les
exigences de qualité ci-dessous. Une tâche courte peut garder critères et plan dans
la réponse. Parallélisme et sous-agents seulement si utiles, autorisés et réellement
pris en charge ; jamais d'activation systématique.
Appliquer les principes compatibles et les adaptations disponibles, signaler les
capacités manquantes sans les simuler. Une auto-revue n'est pas une revue indépendante.
Intégration et découverte selon le harness : [référence ciblée](docs/agent-workflow.md#intégration-de-superpowers).

## Convention de travail

- Avant écriture : vérifier chemin, branche, SHA, diff, index, worktrees et sous-module.
  Ne pas modifier `main` ni l'espace d'une autre tâche ; conserver les travaux présents.
- Une tâche = une branche depuis `main` : `feat/`, `fix/`, `docs/` ou `chore/`.
  Créer un worktree si l'isolation est nécessaire ou demandée ; une lecture seule
  n'en exige pas. Pour les tâches concurrentes : `../bot-radar-worktrees/<sujet>/`.
  Ne pas réutiliser automatiquement un chemin ou une branche ; en cas d'échec
  d'isolation, ne pas se rabattre sur le dossier principal.
- Sans réseau autorisé, qualifier la base de locale. Ne pas basculer ou actualiser
  automatiquement le dossier principal. Aucun worktree n'isole les services ou ports.
- Commit, push, PR et nettoyage nécessitent un mandat les autorisant ; merge et
  déploiement exigent une demande explicite. Aucun force-push ni contournement de
  protection de branche.
- La livraison autorisée passe par branche et PR vers `main`, merge squash.
  Conserver les contrôles locaux adaptés et la CI `check` et `test-postgres` vertes
  sur une branche à jour. Un push seul n'invalide pas les résultats locaux ;
  leur réutilisation suit la politique de vérification ci-dessous. `make verify`
  attend uniquement la CI et nécessite le réseau.
- Avant tout merge, présenter le commit de la PR, un résumé court des changements,
  résultats et risques ou limites, puis demander : « Autorises-tu le merge de la PR #X ? »
  L'accord explicite autorise uniquement le merge de cette PR au commit présenté,
  sans revue humaine formelle ni commentaire d'approbation supplémentaire. Si le
  contenu change, demander une nouvelle confirmation. L'auto-revue reste identifiée
  comme telle ; l'accord n'atteste pas une revue du code par l'utilisateur.
  Cette règle vaut aussi pour les chemins sensibles. Elle n'autorise ni déploiement
  ni opération sensible supplémentaire. Si GitHub exige une approbation formelle,
  signaler le blocage : toute modification des protections exige une autorisation
  administrative distincte, sans contournement.
- Pour toute opération Git, isolation, passation ou découverte de skills, lire
  [le workflow](docs/agent-workflow.md). Ne pas stash, déplacer ou supprimer le
  travail existant. Aucun prune ou retrait forcé.

## Invariant 1 — Telegram d'abord

- Chemin : dédoublonnage `(source_name, external_id)` → matching CPU local →
  cote en cache local → envoi Telegram → enrichissements en arrière-plan.
- Aucun appel BNF, marketplace ou moteur IA ajouté avant l'envoi. L'état durable
  de notification se lit en bloc par annonce, pas un `SELECT` par alerte.
- Toute BD envoyée garde un bloc chiffré ou une ligne expliquant l'état à l'envoi.
  Ce message est un instantané : aucune promesse de mise à jour de cote.
- L'IA utilise le vrai `message_id` depuis `telegram_service.py`. Ne pas réintroduire
  `AsyncEnrichListingUseCase.schedule_enrichment` avec un identifiant fictif.
- La liaison bibliographique utilise une autre session ; la cotation de fond part
  après toutes les alertes. Toute écriture de cote passe par `book_market_pricing`.
- La création BD attend sa préparation en pause avant activation ; ce flux est
  distinct de l'ingestion, qui ne consulte que le cache avant l'envoi.
- Lire le [contrat d'alerte](docs/contracts/alert-pipeline.md) pour ce chemin et ses preuves.

## Invariant 2 — un seul scheduler

- Un processus `uvicorn app.main:app` dans `radar_app` : API, bot, APScheduler et poller.
  APScheduler démarre toujours ; seul le poller est optionnel. Aucun `app.worker`.
- Jobs : `max_instances=1`, `coalesce=True`. Pas de verrou Redis de scheduler
  tant que l'architecture reste à un processus.

## Invariant 3 — secrets et accès

- Ne jamais afficher, copier, committer ou journaliser de secret. Préserver la
  rédaction du `JSONFormatter`. Ne pas lire les `.env` réels pour vérifier le code.
- En production, préserver `validate_production_secrets()` : les quatre tokens
  d'accès sont obligatoires. Valider avant la configuration Telegram et les producteurs.
- Conserver l'allowlist Telegram et les secrets webhook/Bearer. Deux secrets vides
  peuvent être égaux avec `compare_digest` : la validation de démarrage est indispensable.
- Pour vérifier : configuration factice avant imports, `.env` et secrets-directory
  neutralisés, transports externes refusés ou doublés, sous-processus compris.
  Des tokens factices seuls ne suffisent pas. Aucun service réel sans mandat.

## Invariant 4 — fournisseur IA interchangeable

- Seuls `app/providers/llm/` et la configuration connaissent le fournisseur.
  Le reste parle à `LLMClient`. Pas de fournisseur ou modèle dans les signatures,
  textes utilisateur, logs ou noms de tests hors de cette frontière.
- Un seul POST par complétion dans `BaseLLMClient.complete`, HTTP partagé ; pas de
  client parallèle dans un module IA. Préserver le contrat testé sur chaque adaptateur.
- Le repli `None` ne doit pas transformer une absence de réponse en avis du moteur.

## Limites sensibles et qualité

- Aucun paiement, confirmation d'achat ou choix de relais automatisé. Aucune
  interaction marketplace automatisée, aucun userscript, extension ou script
  injecté. Aucun contournement anti-bot. Ne promettre ni absence de risque
  contractuel ni succès authentifié non observé. Exception : le collecteur
  Leboncoin, dans le périmètre défini par ADR-054 (flux Livres BD/manga).
- Sans demande explicite, ne pas étendre ou améliorer `vinted_scraper.py`,
  `sync_vinted_session.py`, `chasse_aux_livres.py`, les formules de prix ou de marché.
  Les exceptions de preuve ISBN et filtres communs sont dans le contrat marché.
- Les scripts de session, collecte et maintenance listés dans
  [le workflow](docs/agent-workflow.md#scripts-sensibles) nécessitent aussi une demande
  explicite avant modification.
- Respecter les couches existantes : domaine pur, providers sans SQL, repositories
  sans matching ni HTML. Ne pas inventer une couche ni déplacer `tome_extractor.py`.
- Améliorer le chemin existant : pas de doublon, code mort, flag orphelin, refactor
  opportuniste ou optimisation lourde sans benchmark reproductible.
- Français pour les messages et les docstrings métier autorisées, identifiants anglais. Pas de
  `time.sleep` dans `app/`. Utiliser `safe_create_task`, le client HTTP partagé et
  le pool Redis de `app.core.redis`. Les enums métier ont une seule définition.
- Ne pas introduire de base globale partagée non isolée dans `conftest.py`.
- Ne supprimer ni affaiblir les tests sans accord explicite. Une exception passée
  ne donne aucune permission nouvelle ; conserver sa trace dans les preuves historiques.

### Pas de commentaires explicatifs ajoutés par défaut

- Ne pas ajouter de commentaires explicatifs ni de docstrings narratives dans le code.
  Privilégier des noms explicites, des fonctions lisibles et des tests exprimant le
  comportement attendu.
- Pour comprendre ou modifier une implémentation, examiner directement le code et
  les tests concernés. Un commentaire ou une description documentaire ne constitue
  pas une preuve du comportement actuel.
- Ne pas raconter dans le code les modifications effectuées, les anciennes
  implémentations, le raisonnement de l'agent ou les étapes évidentes.
- Si une explication semble indispensable pour éviter une erreur non évidente,
  la signaler dans le bilan et demander une exception avant de l'ajouter au code.
- Cette règle ne concerne pas les mentions légales, licences, directives d'outils
  ou annotations nécessaires au fonctionnement. Elle n'autorise ni leur suppression,
  ni celle des commentaires et docstrings existants.
- Les contrats produit, décisions et procédures utiles restent dans la documentation
  spécialisée, sans recopier l'implémentation.

## Vérification et définition de terminé

Choisir les contrôles selon le changement, dans un environnement autorisé et isolé :

| Changement | Contrôles locaux attendus |
| --- | --- |
| Prose descriptive seule | Diff, liens actifs modifiés et tests documentaires concernés ; ni suite applicative ni mypy systématiques |
| Instructions actives | Cohérence des règles, permissions, recettes et tests des garanties concernées ; suite complète si l'impact reste incertain |
| Procédure sensible | Contrats, permissions, ordre des opérations et scénarios d'échec avec doubles ; toute exécution réelle exige son mandat |
| Code, y compris outillage exécutable | Ruff ciblé et tests des consommateurs pendant le travail ; validation complète finale de la branche |

Pour les commits intermédiaires autorisés, les contrôles ciblés suffisent si leur
sélection est justifiée ; élargir si les consommateurs ou les effets sont incertains.
Un commit intermédiaire n'est alors pas attesté par toute la suite. La validation
complète finale d'une branche de code reste obligatoire :

```sh
ruff check .
mypy app scripts/matching_diagnostics.py
make test-isolated
```

`make lint` combine Ruff et mypy ; `make test-isolated` réutilise le lanceur existant
avec configuration factice et transports isolés. La CI complète est conservée.
Pour ingestion → matching → Telegram : vérifier ordre, idempotence, reprise,
versions et sessions. SQL/concurrence exigent les contrôles PostgreSQL concernés ;
migrations, dépendances et exploitation gardent leurs contrôles adaptés. Sans
ressources autorisées, signaler la preuve manquante, sans la remplacer par SQLite
ou un double ni revendiquer une validation réelle.

Réutiliser un résultat seulement si son périmètre est démontrable et si le code,
les tests, la base, le verrou, la configuration de validation et l'environnement
pertinents sont inchangés. Rapporter la commande, le résultat, la révision et le
diff concernés ; aucun registre supplémentaire n'est requis. Un push seul ne
justifie aucun rejeu. Si un élément pertinent change, rejouer les contrôles affectés ;
en cas de doute sur l'impact ou sur la preuve disponible, élargir ou rejouer.

`make check-history` reste optionnel pour une branche, si l'on veut attester chaque
commit par les contrôles complets. Il exige une base nommée et l'autorisation de ses
worktrees temporaires et de son environnement hérité. Le job complet des tags est
conservé ; la validation finale ne garantit pas un historique entièrement vert.

Terminé = contrôles requis verts, critères vérifiés, aucun secret exposé, aucun
réseau ajouté avant Telegram, auto-revue du diff et de l'index effectuée. Un contrôle
hors mandat reste non exécuté et signalé. Rapporter commandes, SHA et diff,
résultats, portée, limites, reste à faire et état `terminé`, `partiel` ou `bloqué`.
Un résultat local n'atteste pas la production. La confirmation de merge suit la
convention de travail ; elle ne remplace aucun contrôle technique requis.

## Navigation selon la tâche

Lire seulement les documents liés au périmètre et à ses dépendances.

| Quand | Référence canonique |
| --- | --- |
| Situer les couches, démarrage, stockage, scheduler | [Architecture](docs/architecture.md) |
| Permissions, Git, worktrees, découverte et reprise | [Workflow agents](docs/agent-workflow.md) |
| Ingestion, matching, envoi, idempotence, latence | [Contrat d'alerte](docs/contracts/alert-pipeline.md) |
| ISBN, séries, offres, validité des références | [Catalogue et marché](docs/contracts/catalogue-market.md) |
| Claviers, variantes, auto-tuning et IA | [Telegram et IA](docs/contracts/telegram-ai.md) |
| Préparation durable BD, pause, édition ISBN | [Préparation](docs/bd-preparation.md), [pause](docs/pause-actions.md), [ISBN](docs/isbn-removal.md) |
| Déploiement autorisé, interruption ou retour | [Déploiement](docs/operations/deployment.md), puis procédure BD liée |
| Sauvegarde/restauration autorisée | [Récupération](docs/backup-recovery.md), [pause](docs/pause-actions.md) |
| Comprendre une décision et ses remplacements | [Index ADR](docs/decisions/README.md) |
| Écarts connus ou limites de preuve | [Points ouverts](docs/open-issues.md) |

## Maintenance documentaire

Modifier un contrat implique de revoir sa documentation canonique et ses consommateurs.
Ne pas actualiser toutes les docs pour chaque changement. Remplacer ou supprimer
une instruction périmée au lieu d'empiler une exception ; dater et borner les preuves
historiques.
