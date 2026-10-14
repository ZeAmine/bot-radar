# Validation transversale locale — 2026-10-12

Le bilan initial est conservé comme historique. Le résultat actualisé et la qualification des 79 échecs figurent dans « Réalignement autorisé des tests » ci-dessous : validation locale réussie, dans la portée synthétique précisée.

Référence applicative testée : `bfabcca7dbf4f8f6af2de058b167a93dd80816b8`, sur main. Aucun fichier applicatif, migration, fixture partagée ou assertion métier modifié dans cette passe.

**Verdict : validation incomplète.** La suite entière a été sélectionnée par backend, sans désélection, skip ou xfail ajouté. Elle est rouge. Aucun commit de vérification créé : les seuls nouveaux fichiers restent locaux.

## Retrait ciblé du garde d’autonomie (13 octobre 2026)

Les résultats historiques de ce document sont conservés ; ils ne sont pas une
nouvelle exécution. Leur manifeste d’origine est consultable à la révision
`a303223cf1eff6c1308c7e492b69e6f0572f9d63` dans l’historique Git.
Le manifeste de travail `docs/consolidation-nodeids.json` retire maintenant
uniquement les 59 nodeids des deux modules dédiés au garde supprimé,
par exception explicitement validée. Ses 1706 entrées restantes sont
inchangées et dans le même ordre ; les compteurs historiques ci-dessous ne sont
pas recalculés. Aucun autre scénario n’est supprimé ou désélectionné.

## Consolidation du cadre — 14 octobre 2026

Cette section décrit le retrait et son diagnostic ; les résultats historiques
ci-dessous gardent leur date, leur révision et leur portée. La politique active
reste uniquement dans [AGENTS.md](../AGENTS.md#pilotage-autonome-borné).

### Retrait et contrôles conservés

- [F] La PR #98 retire `scripts/task_verdict.py`, `scripts/task_guard.py`, leur
  gabarit TOML et leurs seuls tests dédiés. La notice transitoire
  `docs/autonomy.md` est également retirée ; ses précautions sont conservées ici.
- [F] Le code retiré portait l'empreinte de la fiche approuvée et des scripts de
  confiance, les périmètres de fichiers et de commandes, les compteurs persistés
  de durée/cycles/appels, les contrôles de tests et de diff, le digest avant/après
  preuve, les décisions des hooks et l'état de reprise. Ces contrôles spécifiques
  ne seront plus fournis par ces scripts. Leur présence ne prouvait pas leur
  activation dans un IDE ; leur retrait n'améliore pas automatiquement la sécurité.
- [F] `scripts/verify_consolidation.py`, son manifeste général et ses protections
  de recette restent conservés. La CI définit Ruff, mypy, pytest, le contrôle du
  verrou et l'intégration PostgreSQL ; aucun de ces contrôles n'est retiré.
- [F] La consultation GitHub du 14 octobre donne `main` à
  `d957de724d114db3a3c06cf71955f89bcef09fe9`, la PR #98 à
  `301516ac4e0e9ece48caadff05910539e9ebf0c5` et son état `BEHIND`.
  La protection consultée exige `check`, `strict=true`, s'applique aux
  administrateurs et refuse le force-push. Cette lecture n'est pas un essai de push.
- [F] La PR #112 recouvre ce retrait ; elle n'est ni modifiée ni fermée dans ce lot.

### Frontières de l'exécution

- [F] Le worktree sépare les fichiers de la tâche ; il n'isole pas les processus,
  dépendances, ports, bases, Redis, conteneurs ou volumes. La recette conservée
  neutralise les sources dotenv/secrets-directory avant les imports, refuse les
  transports non autorisés et propage une garde aux enfants Python. Ce mécanisme
  de test n'est pas une sandbox générale d'agent ou de code natif.
- [F] La session déclare des racines d'écriture limitées et un réseau restreint,
  avec escalade soumise à revue. Les premières consultations Git/GitHub ont échoué
  sur la résolution/connexion ; leur reprise autorisée a réussi. Cela ne prouve
  ni un filtrage réseau exhaustif ni une interdiction technique de la production.
- [F] Le profil fourni à cette session autorise largement la lecture du filesystem ;
  il ne déclare pas de cloisonnement des secrets. Des outils/MCP capables d'effets
  externes sont exposés. Leur présence n'autorise pas leur usage dans ce mandat et
  ne prouve pas qu'un garde externe intercepte chacun de leurs appels.
- [F] L'accès au socket Docker a d'abord été refusé ; sa consultation autorisée
  avec escalade a réussi. Les écritures documentaires du worktree et le cache
  mypy ont également nécessité l'accès hors des racines d'écriture par défaut.
  Ce sont des observations locales, pas une frontière infranchissable attestée.
- [I] L'accès effectif aux secrets, clés SSH et ressources de production n'a pas
  été éprouvé : aucun secret ni fichier d'authentification n'est lu, aucune
  connexion de production n'est tentée. Le mandat interdit ces accès ; il ne les
  rend pas techniquement impossibles.
- [I] L'activation actuelle des hooks personnels Cursor et la médiation de chaque
  outil/MCP ne sont pas établies. Les fiches, approbations, journaux et rapports
  historiques hors dépôt restent à leur place, comme les anciens worktrees.
- [F] Les règles et validateurs versionnés sont modifiables par une tâche qui en
  reçoit la permission. Leur intégrité dépend aussi de la revue et des protections
  externes ; le texte d'AGENTS.md ne les rend pas immuables.
- [F] Le lanceur de recette contient des délais et la terminaison du groupe de
  processus qu'il lance. [I] L'arrêt réel de tous les outils, processus détachés
  et appels MCP d'une session complète n'est pas établi. Aucun plafond global de
  durée, tentatives ou consommation n'est attesté par le dépôt.
- [R] Qualifier l'usage d'**autonomie encadrée à usage supervisé** tant que ces
  frontières et la reprise après interruption ne sont pas validées. Aucun défaut
  du bot métier n'est déduit de ces informations manquantes.

### Actions hors dépôt proposées, non appliquées

- [R] Faire contrôler par l'opérateur les hooks qui viseraient les scripts retirés,
  terminer ou suspendre leurs sessions puis désactiver uniquement ces raccordements.
  Test : une tâche synthétique ne doit plus appeler un chemin supprimé. Ne déplacer
  ni supprimer les états historiques pendant cette transition.
- [R] Prévoir un profil d'exécution sans montage de secrets ni clé de production,
  avec périmètres filesystem et réseau imposés hors de l'arbre modifiable.
  Test : lire un fichier témoin autorisé puis tenter un témoin synthétique hors
  périmètre ; utiliser une destination locale factice pour éprouver le refus réseau.
  Aucun secret ni hôte de production n'est nécessaire à ces essais.
- [R] Restreindre ou désactiver les outils/MCP à effets externes et protéger les
  validateurs de confiance hors de l'arbre accessible. Test : tenter une opération
  sur une ressource factice et une modification d'un validateur témoin ; vérifier
  le refus avant tout effet. Ne pas utiliser de ressource réelle pour ce test.
- [R] Configurer dans le superviseur existant des plafonds imposés et l'arrêt du
  groupe de processus, puis tester avec un sous-processus local temporaire que
  l'interruption arrête aussi l'enfant. Vérifier séparément les métriques de
  consommation : un compteur déclaré n'est pas une mesure observée.
- [R] Éprouver une interruption/reprise sur une tâche synthétique en conservant
  mandat, SHA, diff/index, preuves, ressources et prochaine étape, selon AGENTS.md.
  La reprise doit détecter un arbre modifié et ne pas réutiliser une preuve périmée.

### Vérifications de ce complément (14 octobre 2026)

**Verdict du complément : partiel.** Le retrait et sa revue sont réalisés ; la
recette SQLite n'est pas verte et la branche est derrière `main`. La validation
complète n'est donc pas acquise, même si les checks GitHub du nouveau SHA passent.

Arbre de `chore/remove-agent-autonomy`, parent `301516a`, avec uniquement les
compléments documentaires de ce lot. Python 3.12.0 ; runtime temporaire sans pip
ni fichiers `.pth`/editable, dépendances déjà présentes exposées par liens, source
`app` résolue dans le worktree. Aucun paquet, outil ou sous-module installé ou mis
à jour. Chaque commande de recette reçoit un environnement parent vide ; les
barrières sont installées avant les imports applicatifs, enfants Python compris.

| Commande / contrôle | Résultat observé | Portée |
| --- | --- | --- |
| `ruff check --no-cache .` | vert | Arbre complet de cette branche |
| `python scripts/check_lock.py` | vert, 18 dépendances couvertes | Cohérence déclarations/verrou, pas une installation |
| `python scripts/verify_consolidation.py typecheck` | vert, 167 + 4 fichiers | Contrat mypy et outils auxiliaires |
| `python scripts/verify_consolidation.py unit` | vert, 1396 tests | Unitaires/performance, aucun baseline manquant |
| Même commande avec `--target tests/performance`, seule | vert, 6 tests | Rejouée sans vérification concurrente |
| `python scripts/verify_consolidation.py postgres` | vert, 326 tests | PostgreSQL jetable, installation vierge et chaîne peuplée 0010→0013 ; conteneur supprimé par son ID |
| Même recette `unit --target tests/integration` | pytest : 314 réussis, 12 skips PostgreSQL ; lanceur : sortie 1 | Assertion finale « Scénario incomplet » : ces skips n'ont pas de phase `call`. Limite du lanceur inchangé, aucun test adapté |
| Manifeste comparé à `a303223` et `git diff --check` | verts | Exactement 59 nodeids dédiés retirés, 1706 conservés dans l'ordre ; références restantes historiques uniquement |
| Revue indépendante en lecture seule | aucun défaut actionnable relevé | Retrait et compléments, sans attestation de l'environnement global |

Chaque recette achevée compte une sonde dotenv refusée et deux sondes réseau
refusées, sans tentative accidentelle ; les enfants Python ne déclarent aucun
accès interdit. Deux avertissements de dépendances (Starlette/httpx et Alembic)
sont conservés. Le premier mypy a échoué à créer son cache hors des racines
d'écriture ; la relance avec accès autorisé a passé. La première performance
chevauchait d'autres contrôles et n'est pas retenue comme mesure ; seule sa
réexécution isolée l'est.

Aucun contrôle de production, essai réel des permissions/hook/MCP, plafond global,
arrêt complet de session ou reprise synthétique n'a été exécuté. Aucun
`check-history` supplémentaire n'est lancé : il crée d'autres worktrees et ne
fait pas partie des contrôles autorisés ici. La branche reste derrière `main` ;
les preuves locales concernent cet arbre, pas une future intégration. Les checks
GitHub du SHA livré et le verdict actualisé sont rapportés dans la PR ; les
anciens résultats CI ne valent pas validation du nouveau commit.

## Exécutions finales

| Exécution | Collectés | Réussis | Échoués | Skips existants | Désélectionnés |
|---|---:|---:|---:|---:|---:|
| Unitaires et performance, SQLite | 1439 | 1398 | 41 | 0 | 0 |
| Intégration, PostgreSQL 16 | 326 | 288 | 38 | 0 | 0 |

Les sélections sont disjointes : 1765 nodeids distincts. Les relances exploratoires et le filet webhook ne sont pas additionnés. L’intégration inclut le parent webhook, qui impose son propre sous-processus SQLite fichier : ses 62 scénarios ne constituent pas 62 nouveaux tests de cette collection ni une preuve de verrous PostgreSQL.

```text
.venv/bin/python -B scripts/verify_consolidation.py unit
.venv/bin/python -B scripts/verify_consolidation.py postgres
.venv/bin/python -B scripts/verify_consolidation.py backup
.venv/bin/python -B scripts/verify_consolidation.py typecheck
.venv/bin/python -B tests/integration/test_telegram_webhook_transient_failure.py
.venv/bin/ruff check .
bash -n scripts/backup_db.sh
```

Typage : 167 fichiers app + matching_diagnostics vérifiés sans erreur ; quatre outils vérifiés séparément avec follow-imports=silent, sans erreur. Ruff dépôt et syntaxe Bash réussis. Filet webhook : 14 tests parents et 62 scénarios enfants complets réussis, y compris les rejets de sous-processus incomplets.

Le premier essai large sans échéance par scénario a dépassé son budget global. Les exécutions finales ajoutent une échéance diagnostique de 20 secondes par scénario, sans prolonger les attentes métier ni supprimer leur synchronisation. Une expiration est un échec de validation, pas la preuve d’un bug applicatif.

## Relances exploratoires — non additionnées

| Journal / sélection | Collectés | Réussis | Échoués | Non-applicables pytest | Désélectionnés | Motif de relance |
|---|---:|---:|---:|---:|---:|---|
| unit initial sans échéance individuelle | Rapport final absent | Non comptés | Non comptés | Non comptés | 0 demandé | Arrêt global à 240 s, exit 124 ; enfant terminé et attendu |
| final-unit-guarded, unit+performance | 1439 | 1348 | 91 | 0 | 0 | Transports non simulés bloqués ; faux environnement AI désactivé |
| final-unit-final, unit+performance | 1439 | 1348 | 91 | 0 | 0 | Transports désormais simulés ; AI encore désactivée artificiellement |
| final-unit-final2, unit+performance | 1439 | 1397 | 42 | 0 | 0 | Valeur AI par défaut respectée ; création du fichier .env factice encore bloquée |
| final-unit-complete, unit+performance | 1439 | 1398 | 41 | 0 | 0 | Exécution finale conforme ; aucune lecture dotenv réelle |
| postgres initial | 326 | 288 | 38 | 0 | 0 | Première instance neuve, migrations puis intégration |
| postgres final | 326 | 288 | 38 | 0 | 0 | Nouvelle instance neuve, transports synthétiques conformes |

La réduction des faux négatifs résulte exclusivement du lanceur : conservation de la valeur AI par défaut, simulation explicite de transports indisponibles, autorisation de créer un .env exclusivement factice dans le temporaire sans le lire. Aucun changement applicatif, fixture ou assertion métier. Le premier lanceur de backup a échoué sur son import avant allocation ; le correctif portait uniquement sur le lanceur. Les deux exercices de backup ensuite exécutés ont réussi et nettoyé leurs ressources. Les versions finales n'ont plus de tentative accidentelle dotenv/réseau, mais les quatre scénarios scheduler demeurent bloqués avant démarrage.

## Isolation

Barrières installées avant imports/collecte, et sitecustomize dédié pour les imports à froid des sous-processus Python. Le lanceur sécurisé webhook de référence est intact. Les sources dotenv et secret-directory de Pydantic sont désactivées ; les lectures .env sont bloquées. La configuration versionnée .env.example peut être inspectée ; les écritures seules d’un .env exclusivement synthétique dans le temporaire sont autorisées pour le test CLI qui vérifie justement son absence de lecture.

Transports HTTP et Redis par défaut : échecs synthétiques explicites, sans DNS ni connexion. Les transports MockTransport/ASGI et doubles explicites des tests restent utilisés. Aucun résultat métier n’est fabriqué. Les processus SSH/rsync/installation et Docker réels hors ressources attestées sont refusés.

Chaque exécution finale compte exactement une sonde dotenv et deux sondes réseau refusées, aucune tentative accidentelle de ces accès. Les sous-processus Python gardés n’ont rapporté aucune tentative interdite. Quatre tentatives de démarrage réel APScheduler sont refusées dans les tests de lifespan : ces scénarios apparaissent dans les 41 échecs mais leur partie nécessitant le scheduler réel n’a pas été validée. Aucune autorisation de démarrage n’a été inférée.

## Migrations et interactions

Installation depuis une base vide : upgrade head effectué sur PostgreSQL 16 neuf, graphe unique 0013_pause_actions. Puis reset de cette seule base attestée et création d’une véritable révision 0010 peuplée : identifiants, liens alert_books, dates, préparation, marqueur notifié et pending, cache courant, ancien minimum et snapshot conservés. Chaîne 0010→0011→0012→0013 exécutée et conservation comparée sur les colonnes originales de toutes les tables peuplées.

Après migration : pending unknown, aucun attempt_id/message_id/bot_id inventé ; marqueur notifié conservé ; aucune action de pause rétroactive et versions initiales à zéro ; aucune projection annuelle rétroactive créée. Ce schéma synthétique ne renseigne pas la production.

Le test des deux prix commités concurrents et celui des réservations conditionnelles ont passé setup/call/teardown sur PostgreSQL, sans skip. Les protections de pause, refus des anciens boutons, preuves atomiques, annulations et absence de nouveaux envois au rejeu restent vérifiées par le filet. Les points de démarrage d’un nouveau brouillon prennent le même draft_guard que la consommation ; les dates des cotes ne sont pas des versions de surveillance.

## Échecs qualifiés et limites de diagnostic

| Constat | Preuve / scénario | Classement | Intervention minimale proposée, non appliquée |
|---|---|---|---|
| Tête Alembic attendue 0010 | tests/unit/test_preflight_deploy.py:68 attend 0010 alors que la tête constatée est 0013 | Test obsolète, pas échec de migration | Aligner cette attente sur le graphe réel, avec contrôle indépendant |
| Faux flush/commit sans identifiant d’autorisation | tests/unit/test_bot_handlers.py:184 ; issue_pause_button, app/use_cases/pause_alert.py:132–140, rencontre id=None avec session AsyncMock | Double SQL incomplet ; cas réel de carte persistée passe | Tester l’émission avec base isolée et relire le jeton, sans supprimer la validation |
| Prix historique affiché sans preuve temporelle | test_series_matching_lot_card et matrice lot attendent les anciens labels ; app/services/market_price_service.py:141 refuse les minima non vérifiables | Attentes incompatibles avec point 7 validé | Fournir des observations admissibles datées, ou vérifier explicitement l’indisponibilité |
| Timings d’envoi absents | tests/integration/test_alert_latency.py:60–76 crée chat 601 et bot AsyncMock sans identité exploitable ; admission privée refuse/contraint l’envoi | Fixture incompatible avec admission et reçus persistés | Autoriser le destinataire factice et fournir bot.id/reçu valides ; garder les assertions de séparation des étapes |
| Total 2000 au lieu de 3500 | tests/integration/test_series_membership_totals.py:290 utilise une offre sans observed_at ; contrat courant ne la rend pas admissible | Cas à requalifier contre provenance, pas preuve d’un défaut de verrou | Scénario avec véritable relevé synthétique daté puis contrôle distinct du cas non daté |
| Cycle de vie scheduler réel | tests/unit/test_unified_lifespan.py:172 crée AsyncIOScheduler, refusé par le mandat | Partie non exécutée ; échéance diagnostique, pas défaut applicatif établi | Autorisation bornée distincte ou double fidèle du scheduler pour ces quatre scénarios |

Les autres échecs ci-dessous ne sont pas déclarés automatiquement obsolètes ou applicatifs. La suite n’établit pas encore leur cause exacte. Aucun changement d’attente pour obtenir du vert. Les benchmarks rouges ne démontrent pas un dépassement du chemin réel à eux seuls, et ne sont pas considérés validés.

## Sauvegarde finale

Exercice synthétique final sur 0013 avec chiffrement/déchiffrement, restauration vide, contrôles négatifs, publication locale vérifiée seulement. Deux actions ont été émises : une appliquée par le vrai use case puis suivie d’une reprise, une non consommée. La preuve historique est conservée après restauration ; l’action non consommée est invalidée. Alertes en pause, contexte de préparation conservé, activation_requested=false et token renouvelé ; récupération des préparations sans réactivation, zéro envoi simulé avant revue. Aucun FSM importé.

Dump ~0,100 s ; vérification archive ~0,050 s ; kit ~2,434 s ; chiffrement ~0,031 s ; déchiffrement ~0,021 s ; restauration ~0,103 s ; contrôle données ~0,049 s ; quarantaine ~0,081 s. Volume synthétique minuscule : aucune validation du délai de quatre heures sur données réelles.

Contrôles négatifs : nouvelle sauvegarde échouée, mauvaise clé/cible/version, écrasement, corruption, troncature, manifeste incomplet, cible non vide ; fraîcheur et rétention proposées également couvertes par les tests unitaires existants. Aucun stockage indépendant, planification, purge réelle ou libération métier activé.

backup_db.sh sans option retourne exactement 2 avec “Sauvegarde indépendante non configurée : aucun succès annoncé.” Les appelants existants doivent traiter ce non-zéro et surveiller stderr ; un cron qui les masque pourrait perdre sa visibilité. Leur état réel n’a pas été inspecté.

## État du plan

| Point | État à conserver |
|---|---|
| 1 | Partiel globalement ; sous-correctif pause/reconciliation vérifié, réception et rejeu réels non garantis |
| 2 | Correction locale de validité conservée ; validation transversale des anciens scénarios de prix incomplète |
| 3 | Protection des brouillons conservée ; anciens doubles de création à remettre en cohérence |
| 4 | Repli IA conservé ; tests contrat et confidentialité locaux, pas de fournisseur réel |
| 5 | Correction au processus unique conservée ; pas de garantie crash/FSM/multiprocessus |
| 6 | Reprise bornée conservée ; réservation PostgreSQL passe, plusieurs anciens tests d’envoi rouges |
| 7 | Contrat temporel et rétention conservés ; attentes historiques anciennes rouges à qualifier |
| 8 | Portée des indicateurs conservée ; cycle de vie réel non validé ici |
| 9 | Outillage restauré et quarantainé synthétiquement ; sauvegarde indépendante non opérationnelle |
| 10 | Validation montants/indices conservée ; aucune modification des formules |
| 11 | Masquage conservé, secrets factices seulement ; aucune lecture de secrets réels |

## Prérequis

Avant livraison : qualifier tous les échecs, remettre les doubles/attentes en cohérence sans réduire les assertions, vérifier à nouveau la suite, décider du périmètre des quatre tests scheduler. Aucun push ni déploiement autorisé par ce résultat.

Exploitation : destination et surveillance indépendantes, permissions, responsables de sauvegarde/restauration/alertes, clés privées récupérables hors serveur et rotation ; budget mémoire et délais à mesurer avec volume représentatif. Le dump, bundle, déchiffrement et fingerprints utilisent des buffers et listes en mémoire, parfois plusieurs copies. Les limites de commandes de l’outil doivent être dimensionnées ; les quatre heures et les 24 h demeurent des objectifs.

## Ressources

Les identités finales et intermédiaires figurent ci-dessous ; tous les conteneurs créés ont été supprimés par leur ID exact. Clés et espaces de recette supprimés, connexions fermées. Aucun conteneur ou volume existant réutilisé. Les logs de diagnostic restent exclusivement synthétiques dans /private/tmp.

- `FINAL_RESOURCE {"id": "3771d74c7a287d2f08801572fc4d0d2918543aeb89ac1a458f355d9e676c8370", "system_id": "7695710376451338281", "port": 55514}`
- `FINAL_RESOURCE_REMOVED 3771d74c7a287d2f08801572fc4d0d2918543aeb89ac1a458f355d9e676c8370`
- `FINAL_RESOURCE {"id": "d27078855a01326795e710c56ada6dd9a6fd0aa25c74012d62e0b5f46872f596", "system_id": "7695714024940744745", "port": 55514}`
- `FINAL_RESOURCE_REMOVED d27078855a01326795e710c56ada6dd9a6fd0aa25c74012d62e0b5f46872f596`
- `SYNTHETIC_RESOURCE {"id": "f074db27eb551cb964c2a8832a257ba727cf1b2f7b173458b8148d57e3e5fcc1", "system_id": "7695711614031188008", "port": 55520}`
- `SYNTHETIC_RESOURCE {"id": "8868ed65c1e15a0c9450ffe0fd8246d77e67cbb7193056347b3e5ad6e7adf9a4", "system_id": "7695711618325368872", "port": 55521}`
- `RESOURCE_REMOVED f074db27eb551cb964c2a8832a257ba727cf1b2f7b173458b8148d57e3e5fcc1`
- `RESOURCE_REMOVED 8868ed65c1e15a0c9450ffe0fd8246d77e67cbb7193056347b3e5ad6e7adf9a4`
- `SYNTHETIC_RESOURCE {"id": "59591c5cdd2e5af5a8a332eabad5a8c7e5e39114da92a39cb42d465969d4e109", "system_id": "7695716278101925928", "port": 55520}`
- `SYNTHETIC_RESOURCE {"id": "0d1f4b58898e5ec21dc2f08781c566b107b802105f34164ac26974b9c546f39c", "system_id": "7695716283016400936", "port": 55521}`
- `RESOURCE_REMOVED 59591c5cdd2e5af5a8a332eabad5a8c7e5e39114da92a39cb42d465969d4e109`
- `RESOURCE_REMOVED 0d1f4b58898e5ec21dc2f08781c566b107b802105f34164ac26974b9c546f39c`

## Échecs exacts — unit

- `tests/performance/test_alert_path_benchmark.py::test_benchmark_full_alert_path`
- `tests/performance/test_series_resolution_benchmark.py::test_benchmark_the_alert_is_not_delayed_by_a_slow_series_lookup`
- `tests/unit/test_add_creation_card.py::test_a_just_created_book_still_gets_its_confirmation`
- `tests/unit/test_add_creation_card.py::test_a_missing_identifier_is_told_in_the_confirmation_too`
- `tests/unit/test_add_creation_card.py::test_add_natural_add_applies_variants_without_asking_for_a_click`
- `tests/unit/test_add_creation_card.py::test_add_natural_add_persists_variants_and_exclusions_with_the_alert`
- `tests/unit/test_add_creation_card.py::test_add_natural_add_shows_the_book_found_before_looking_for_variants`
- `tests/unit/test_add_creation_card.py::test_add_recap_is_the_same_card_whatever_the_route`
- `tests/unit/test_add_creation_card.py::test_confirmation_shows_market_price_without_the_marketplace_name`
- `tests/unit/test_add_creation_card.py::test_series_alert_starts_book_resolution_at_creation`
- `tests/unit/test_add_creation_card.py::test_the_card_names_the_alert_exactly_as_the_row_is_named[None]`
- `tests/unit/test_add_creation_card.py::test_the_card_names_the_alert_exactly_as_the_row_is_named[]`
- `tests/unit/test_add_creation_card.py::test_the_draft_note_stays_on_the_recap_and_never_reaches_the_confirmation`
- `tests/unit/test_alert_creation_pricing.py::test_creation_waits_for_price_before_activating_and_confirming[False]`
- `tests/unit/test_alert_creation_pricing.py::test_creation_waits_for_price_before_activating_and_confirming[True]`
- `tests/unit/test_alert_latency_helpers.py::test_photo_transport_retry_pause_and_text_ack_are_separate`
- `tests/unit/test_backfill_series_tomes.py::test_a_second_write_pass_adds_neither_book_link_nor_total`
- `tests/unit/test_backfill_series_tomes.py::test_a_write_pass_links_and_prices_every_tome_of_the_series`
- `tests/unit/test_bot_handlers.py::test_handle_select_edit_category`
- `tests/unit/test_bot_handlers.py::test_handle_show_with_query`
- `tests/unit/test_bot_handlers.py::test_paginate_alerts_with_category_slug`
- `tests/unit/test_charte_copy.py::test_the_confirmation_is_the_recap_read_back_from_the_same_card`
- `tests/unit/test_night_mode.py::test_poller_night_mode_suspension`
- `tests/unit/test_preflight_deploy.py::test_local_alembic_head_matches_the_repository`
- `tests/unit/test_requested_book_volume.py::test_stale_confirmation_does_not_write_an_unconfirmed_volume`
- `tests/unit/test_series_matching.py::test_counted_lot_resolves_to_the_whole_series_and_prices_it`
- `tests/unit/test_series_matching.py::test_format_market_details_card_keeps_missing_tome_visible_in_lot`
- `tests/unit/test_series_matching.py::test_market_details_card_series_tomes_formatting`
- `tests/unit/test_series_matching.py::test_series_matching_chateau_des_animaux_lot_1_2_3`
- `tests/unit/test_series_matching.py::test_series_matching_lot_card`
- `tests/unit/test_series_matching.py::test_series_matching_lot_summary`
- `tests/unit/test_series_matching.py::test_series_matching_lot_variations_matrix[lot_tomes0-2500-<b>Prix bas constat\xe9 (Lot) :</b> ~34,50 \u20ac-\u2705--9,50 \u20ac]`
- `tests/unit/test_series_matching.py::test_series_matching_lot_variations_matrix[lot_tomes1-3000-<b>Prix bas constat\xe9 (Lot) :</b> ~36,00 \u20ac-\u2705--6,00 \u20ac]`
- `tests/unit/test_series_matching.py::test_series_matching_lot_variations_matrix[lot_tomes2-6000-<b>Prix bas constat\xe9 (Lot) :</b> ~54,50 \u20ac-\u26d4\ufe0f-+5,50 \u20ac]`
- `tests/unit/test_series_matching.py::test_series_matching_lot_variations_matrix[lot_tomes3-1500-<b>Prix bas constat\xe9 (Lot) :</b> ~18,50 \u20ac-\u2705--3,50 \u20ac]`
- `tests/unit/test_series_matching.py::test_series_matching_specific_tome_summary`
- `tests/unit/test_series_matching.py::test_single_volume_integrale_uses_its_prepared_price`
- `tests/unit/test_unified_lifespan.py::test_cancelling_shutdown_does_not_recancel_the_poller_cleanup`
- `tests/unit/test_unified_lifespan.py::test_scheduler_cleanup_finishes_before_drain_and_client_closure`
- `tests/unit/test_unified_lifespan.py::test_shutdown_cancellation_still_drains_and_closes_clients[drain]`
- `tests/unit/test_unified_lifespan.py::test_shutdown_cancellation_still_drains_and_closes_clients[http]`

## Échecs exacts — postgres

- `tests/integration/test_alert_keywords_roundtrip.py::test_new_edition_is_prepared_in_database_before_surveillance_resumes[price-ACTIVE]`
- `tests/integration/test_alert_keywords_roundtrip.py::test_new_edition_is_prepared_in_database_before_surveillance_resumes[price-PAUSED]`
- `tests/integration/test_alert_latency.py::test_cancellation_keeps_terminal_duration_and_propagates`
- `tests/integration/test_alert_latency.py::test_diagnostic_and_clock_failures_do_not_change_ack_or_sql_error`
- `tests/integration/test_alert_latency.py::test_duplicate_without_send_and_no_match_are_separate`
- `tests/integration/test_alert_latency.py::test_final_commit_failure_is_separate_from_post_send_recalculation`
- `tests/integration/test_alert_latency.py::test_guard_wait_is_separate_and_measured_before_reads`
- `tests/integration/test_alert_latency.py::test_no_ack_duration_for_unsuccessful_results[rejected]`
- `tests/integration/test_alert_latency.py::test_no_ack_duration_for_unsuccessful_results[unknown]`
- `tests/integration/test_alert_latency.py::test_no_ack_duration_for_unsuccessful_results[unusable]`
- `tests/integration/test_alert_latency.py::test_real_bibliography_background_can_start_before_return`
- `tests/integration/test_alert_latency.py::test_recipient_transport_ack_persistence_and_final_are_distinct`
- `tests/integration/test_alert_latency.py::test_sql_recovery_included_without_second_transport`
- `tests/integration/test_bot_flows.py::test_price_write_recomputes_the_series_total`
- `tests/integration/test_bot_flows.py::test_tome_buttons_allow_switching_without_a_global_series_button`
- `tests/integration/test_seller_isbn.py::test_concurrent_confirmations_same_alert_are_idempotent`
- `tests/integration/test_seller_isbn.py::test_confirmation_follows_all_deliveries_and_uses_notice`
- `tests/integration/test_seller_isbn.py::test_existing_coherent_book_metadata_is_preserved`
- `tests/integration/test_seller_isbn.py::test_invalid_isbn_api_keeps_listing_and_warns[4006381333931]`
- `tests/integration/test_seller_isbn.py::test_invalid_isbn_api_keeps_listing_and_warns[9782369903063]`
- `tests/integration/test_seller_isbn.py::test_invalid_isbn_api_keeps_listing_and_warns[X306406152]`
- `tests/integration/test_seller_isbn.py::test_invalid_isbn_api_keeps_listing_and_warns[invalide]`
- `tests/integration/test_seller_isbn.py::test_late_confirmation_cannot_link_after_user_change[delete]`
- `tests/integration/test_seller_isbn.py::test_late_confirmation_cannot_link_after_user_change[isbn]`
- `tests/integration/test_seller_isbn.py::test_late_confirmation_cannot_link_after_user_change[name]`
- `tests/integration/test_seller_isbn.py::test_late_confirmation_cannot_link_after_user_change[pause]`
- `tests/integration/test_seller_isbn.py::test_late_confirmation_cannot_link_after_user_change[preparation]`
- `tests/integration/test_seller_isbn.py::test_legacy_invalid_isbn_is_ignored_without_rewriting_listing`
- `tests/integration/test_seller_isbn.py::test_unconfirmed_or_foreign_code_never_links_or_prices[confirme-Autre ouvrage]`
- `tests/integration/test_seller_isbn.py::test_unconfirmed_or_foreign_code_never_links_or_prices[inconnu-None]`
- `tests/integration/test_seller_isbn.py::test_unconfirmed_or_foreign_code_never_links_or_prices[silence-Cible BD]`
- `tests/integration/test_series_membership_totals.py::test_real_ingestion_and_market_panel_agree[complete]`
- `tests/integration/test_series_membership_totals.py::test_real_ingestion_and_market_panel_agree[edition]`
- `tests/integration/test_series_membership_totals.py::test_real_ingestion_and_market_panel_agree[expired]`
- `tests/integration/test_series_membership_totals.py::test_real_ingestion_and_market_panel_agree[missing]`
- `tests/integration/test_series_membership_totals.py::test_real_ingestion_and_market_panel_agree[none]`
- `tests/integration/test_series_membership_totals.py::test_real_ingestion_and_market_panel_agree[undated]`
- `tests/integration/test_series_membership_totals.py::test_recalculation_reads_links_after_waiting_for_mutation[price]`


## Réalignement autorisé des tests — 2026-10-12

Ce chapitre remplace le verdict de la première passe pour les sélections réexécutées ; les résultats historiques ci-dessus restent conservés. Référence applicative inchangée : `bfabcca7dbf4f8f6af2de058b167a93dd80816b8`. Aucun fichier applicatif, migration, fixture partagée, AGENTS.md ou lanceur webhook de référence modifié. Le futur commit ne porte que les tests, l’outil final et ce bilan.

**Verdict : validation locale transversale réussie dans les environnements synthétiques exécutés.** Aucun défaut applicatif reproduit dans les 79 échecs qualifiés ; cela ne prouve ni absence générale de défaut ni comportement des services réels.

### Qualification et preuve

Les 79 nodeids rouges historiques ont tous été conservés et ont passé setup/call/teardown sur leur backend final. Les incompatibilités sont séparées ci-dessous ; chaque ligne conserve l’intention du scénario désignée par son nodeid. Un échec initial pouvait cumuler une identité de double manquante et, après sa correction, une attente financière obsolète. Cette succession est explicitée et ne devient pas un défaut applicatif par déduction.

Causes isolation/nettoyage : aucun défaut de cette classe établi dans les 79 ; deux avertissements de coroutine ont néanmoins révélé une double SQL incorrecte dans des scénarios verts. `execute` reste asynchrone, son résultat et `scalar_one_or_none` sont synchrones ; correction locale, aucun avertissement de coroutine à la fin. Prérequis manquant : quatre schedulers dédiés auparavant interdits, désormais autorisés exclusivement pour ces quatre nodeids et fonctions factices du module ; quatre démarrages, aucun actif ni futur pendant après teardown. Aucune exécution de job applicatif.

Les essais ciblés ont reproduit les familles avant adaptations supplémentaires, puis donné : cartes 15/15, création/historique 159/159, cycle de vie 6/6 et performance 6/6. L’intégration ciblée initiale puis ses relances ont montré successivement admission, nombre supplémentaire de commits de réservation, absence de date des doubles financiers et rejet incertain ; les deux sélections complètes finales sont la preuve retenue, sans addition des relances.

| Nodeid historique (intention conservée) | Qualification | Cause reproduite et correction autorisée | Preuve finale |
|---|---|---|---|
| `tests/integration/test_alert_keywords_roundtrip.py::test_new_edition_is_prepared_in_database_before_surveillance_resumes[price-ACTIVE]` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/integration/test_alert_keywords_roundtrip.py::test_new_edition_is_prepared_in_database_before_surveillance_resumes[price-PAUSED]` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/integration/test_alert_latency.py::test_cancellation_keeps_terminal_duration_and_propagates` | Double incomplet / attente obsolète | Admission et identité corrigées ; réservations durables du point 6 incluses dans les temps exacts. Injection vise persistence/final réel. unknown/unusable restent unknown, rejet permanent abandoned, pas de reprise aveugle. | setup/call/teardown réussis |
| `tests/integration/test_alert_latency.py::test_diagnostic_and_clock_failures_do_not_change_ack_or_sql_error` | Double incomplet / attente obsolète | Admission et identité corrigées ; réservations durables du point 6 incluses dans les temps exacts. Injection vise persistence/final réel. unknown/unusable restent unknown, rejet permanent abandoned, pas de reprise aveugle. | setup/call/teardown réussis |
| `tests/integration/test_alert_latency.py::test_duplicate_without_send_and_no_match_are_separate` | Double incomplet / attente obsolète | Admission et identité corrigées ; réservations durables du point 6 incluses dans les temps exacts. Injection vise persistence/final réel. unknown/unusable restent unknown, rejet permanent abandoned, pas de reprise aveugle. | setup/call/teardown réussis |
| `tests/integration/test_alert_latency.py::test_final_commit_failure_is_separate_from_post_send_recalculation` | Double incomplet / attente obsolète | Admission et identité corrigées ; réservations durables du point 6 incluses dans les temps exacts. Injection vise persistence/final réel. unknown/unusable restent unknown, rejet permanent abandoned, pas de reprise aveugle. | setup/call/teardown réussis |
| `tests/integration/test_alert_latency.py::test_guard_wait_is_separate_and_measured_before_reads` | Double incomplet / attente obsolète | Admission et identité corrigées ; réservations durables du point 6 incluses dans les temps exacts. Injection vise persistence/final réel. unknown/unusable restent unknown, rejet permanent abandoned, pas de reprise aveugle. | setup/call/teardown réussis |
| `tests/integration/test_alert_latency.py::test_no_ack_duration_for_unsuccessful_results[rejected]` | Double incomplet / attente obsolète | Admission et identité corrigées ; réservations durables du point 6 incluses dans les temps exacts. Injection vise persistence/final réel. unknown/unusable restent unknown, rejet permanent abandoned, pas de reprise aveugle. | setup/call/teardown réussis |
| `tests/integration/test_alert_latency.py::test_no_ack_duration_for_unsuccessful_results[unknown]` | Double incomplet / attente obsolète | Admission et identité corrigées ; réservations durables du point 6 incluses dans les temps exacts. Injection vise persistence/final réel. unknown/unusable restent unknown, rejet permanent abandoned, pas de reprise aveugle. | setup/call/teardown réussis |
| `tests/integration/test_alert_latency.py::test_no_ack_duration_for_unsuccessful_results[unusable]` | Double incomplet / attente obsolète | Admission et identité corrigées ; réservations durables du point 6 incluses dans les temps exacts. Injection vise persistence/final réel. unknown/unusable restent unknown, rejet permanent abandoned, pas de reprise aveugle. | setup/call/teardown réussis |
| `tests/integration/test_alert_latency.py::test_real_bibliography_background_can_start_before_return` | Double incomplet / attente obsolète | Admission et identité corrigées ; réservations durables du point 6 incluses dans les temps exacts. Injection vise persistence/final réel. unknown/unusable restent unknown, rejet permanent abandoned, pas de reprise aveugle. | setup/call/teardown réussis |
| `tests/integration/test_alert_latency.py::test_recipient_transport_ack_persistence_and_final_are_distinct` | Double incomplet / attente obsolète | Admission et identité corrigées ; réservations durables du point 6 incluses dans les temps exacts. Injection vise persistence/final réel. unknown/unusable restent unknown, rejet permanent abandoned, pas de reprise aveugle. | setup/call/teardown réussis |
| `tests/integration/test_alert_latency.py::test_sql_recovery_included_without_second_transport` | Double incomplet / attente obsolète | Admission et identité corrigées ; réservations durables du point 6 incluses dans les temps exacts. Injection vise persistence/final réel. unknown/unusable restent unknown, rejet permanent abandoned, pas de reprise aveugle. | setup/call/teardown réussis |
| `tests/integration/test_bot_flows.py::test_price_write_recomputes_the_series_total` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/integration/test_bot_flows.py::test_tome_buttons_allow_switching_without_a_global_series_button` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_concurrent_confirmations_same_alert_are_idempotent` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_confirmation_follows_all_deliveries_and_uses_notice` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_existing_coherent_book_metadata_is_preserved` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_invalid_isbn_api_keeps_listing_and_warns[4006381333931]` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_invalid_isbn_api_keeps_listing_and_warns[9782369903063]` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_invalid_isbn_api_keeps_listing_and_warns[X306406152]` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_invalid_isbn_api_keeps_listing_and_warns[invalide]` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_late_confirmation_cannot_link_after_user_change[delete]` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_late_confirmation_cannot_link_after_user_change[isbn]` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_late_confirmation_cannot_link_after_user_change[name]` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_late_confirmation_cannot_link_after_user_change[pause]` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_late_confirmation_cannot_link_after_user_change[preparation]` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_legacy_invalid_isbn_is_ignored_without_rewriting_listing` | Attente obsolète / double incomplet | Admission et bot identifiés ; rejet certain avec retry_after remplace un False incertain. Backoff écoulé explicitement, ready durable vérifié ; aucun unknown réactivé. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_unconfirmed_or_foreign_code_never_links_or_prices[confirme-Autre ouvrage]` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_unconfirmed_or_foreign_code_never_links_or_prices[inconnu-None]` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_seller_isbn.py::test_unconfirmed_or_foreign_code_never_links_or_prices[silence-Cible BD]` | Double incomplet | Destinataire factice 601/602 autorisé, bot identifié, reçu exploitable ; assertions ISBN, décisions récentes et effets persistés conservées. | setup/call/teardown réussis |
| `tests/integration/test_series_membership_totals.py::test_real_ingestion_and_market_panel_agree[complete]` | Double / données synthétiques incomplets | Admission du destinataire factice et bot identifié ; scénario price doté de la date du relevé. Cas expired, missing, none et undated conservés. | setup/call/teardown réussis |
| `tests/integration/test_series_membership_totals.py::test_real_ingestion_and_market_panel_agree[edition]` | Double / données synthétiques incomplets | Admission du destinataire factice et bot identifié ; scénario price doté de la date du relevé. Cas expired, missing, none et undated conservés. | setup/call/teardown réussis |
| `tests/integration/test_series_membership_totals.py::test_real_ingestion_and_market_panel_agree[expired]` | Double / données synthétiques incomplets | Admission du destinataire factice et bot identifié ; scénario price doté de la date du relevé. Cas expired, missing, none et undated conservés. | setup/call/teardown réussis |
| `tests/integration/test_series_membership_totals.py::test_real_ingestion_and_market_panel_agree[missing]` | Double / données synthétiques incomplets | Admission du destinataire factice et bot identifié ; scénario price doté de la date du relevé. Cas expired, missing, none et undated conservés. | setup/call/teardown réussis |
| `tests/integration/test_series_membership_totals.py::test_real_ingestion_and_market_panel_agree[none]` | Double / données synthétiques incomplets | Admission du destinataire factice et bot identifié ; scénario price doté de la date du relevé. Cas expired, missing, none et undated conservés. | setup/call/teardown réussis |
| `tests/integration/test_series_membership_totals.py::test_real_ingestion_and_market_panel_agree[undated]` | Double / données synthétiques incomplets | Admission du destinataire factice et bot identifié ; scénario price doté de la date du relevé. Cas expired, missing, none et undated conservés. | setup/call/teardown réussis |
| `tests/integration/test_series_membership_totals.py::test_recalculation_reads_links_after_waiting_for_mutation[price]` | Double / données synthétiques incomplets | Admission du destinataire factice et bot identifié ; scénario price doté de la date du relevé. Cas expired, missing, none et undated conservés. | setup/call/teardown réussis |
| `tests/performance/test_alert_path_benchmark.py::test_benchmark_full_alert_path` | Mesure invalide : admission synthétique incomplète | Destinataire autorisé et bot identifié : le chemin mesuré réalise maintenant réellement son envoi simulé. Budgets, nombre d’alertes, échauffement et itérations inchangés. | setup/call/teardown réussis |
| `tests/performance/test_series_resolution_benchmark.py::test_benchmark_the_alert_is_not_delayed_by_a_slow_series_lookup` | Mesure invalide : admission synthétique incomplète | Destinataire autorisé et bot identifié : le chemin mesuré réalise maintenant réellement son envoi simulé. Budgets, nombre d’alertes, échauffement et itérations inchangés. | setup/call/teardown réussis |
| `tests/unit/test_add_creation_card.py::test_a_just_created_book_still_gets_its_confirmation` | Double incomplet | Identité MemoryStorage/StorageKey locale nécessaire au verrou du point 5. Confirmation : projection datée synthétique et libellé annuel du point 7 ; données et état FSM inchangés. | setup/call/teardown réussis |
| `tests/unit/test_add_creation_card.py::test_a_missing_identifier_is_told_in_the_confirmation_too` | Double incomplet | Identité MemoryStorage/StorageKey locale nécessaire au verrou du point 5. Confirmation : projection datée synthétique et libellé annuel du point 7 ; données et état FSM inchangés. | setup/call/teardown réussis |
| `tests/unit/test_add_creation_card.py::test_add_natural_add_applies_variants_without_asking_for_a_click` | Double incomplet | Identité MemoryStorage/StorageKey locale nécessaire au verrou du point 5. Confirmation : projection datée synthétique et libellé annuel du point 7 ; données et état FSM inchangés. | setup/call/teardown réussis |
| `tests/unit/test_add_creation_card.py::test_add_natural_add_persists_variants_and_exclusions_with_the_alert` | Double incomplet | Identité MemoryStorage/StorageKey locale nécessaire au verrou du point 5. Confirmation : projection datée synthétique et libellé annuel du point 7 ; données et état FSM inchangés. | setup/call/teardown réussis |
| `tests/unit/test_add_creation_card.py::test_add_natural_add_shows_the_book_found_before_looking_for_variants` | Double incomplet | Identité MemoryStorage/StorageKey locale nécessaire au verrou du point 5. Confirmation : projection datée synthétique et libellé annuel du point 7 ; données et état FSM inchangés. | setup/call/teardown réussis |
| `tests/unit/test_add_creation_card.py::test_add_recap_is_the_same_card_whatever_the_route` | Double incomplet | Identité MemoryStorage/StorageKey locale nécessaire au verrou du point 5. Confirmation : projection datée synthétique et libellé annuel du point 7 ; données et état FSM inchangés. | setup/call/teardown réussis |
| `tests/unit/test_add_creation_card.py::test_confirmation_shows_market_price_without_the_marketplace_name` | Double incomplet | Identité MemoryStorage/StorageKey locale nécessaire au verrou du point 5. Confirmation : projection datée synthétique et libellé annuel du point 7 ; données et état FSM inchangés. | setup/call/teardown réussis |
| `tests/unit/test_add_creation_card.py::test_series_alert_starts_book_resolution_at_creation` | Double incomplet | Identité MemoryStorage/StorageKey locale nécessaire au verrou du point 5. Confirmation : projection datée synthétique et libellé annuel du point 7 ; données et état FSM inchangés. | setup/call/teardown réussis |
| `tests/unit/test_add_creation_card.py::test_the_card_names_the_alert_exactly_as_the_row_is_named[None]` | Double incomplet | Identité MemoryStorage/StorageKey locale nécessaire au verrou du point 5. Confirmation : projection datée synthétique et libellé annuel du point 7 ; données et état FSM inchangés. | setup/call/teardown réussis |
| `tests/unit/test_add_creation_card.py::test_the_card_names_the_alert_exactly_as_the_row_is_named[]` | Double incomplet | Identité MemoryStorage/StorageKey locale nécessaire au verrou du point 5. Confirmation : projection datée synthétique et libellé annuel du point 7 ; données et état FSM inchangés. | setup/call/teardown réussis |
| `tests/unit/test_add_creation_card.py::test_the_draft_note_stays_on_the_recap_and_never_reaches_the_confirmation` | Double incomplet | Identité MemoryStorage/StorageKey locale nécessaire au verrou du point 5. Confirmation : projection datée synthétique et libellé annuel du point 7 ; données et état FSM inchangés. | setup/call/teardown réussis |
| `tests/unit/test_alert_creation_pricing.py::test_creation_waits_for_price_before_activating_and_confirming[False]` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_alert_creation_pricing.py::test_creation_waits_for_price_before_activating_and_confirming[True]` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_alert_latency_helpers.py::test_photo_transport_retry_pause_and_text_ack_are_separate` | Attente obsolète | Échec réseau inconnu ne justifie pas un second envoi. Rejet photo certain utilisé pour tester repli et délais ; nouveau cas réseau impose un seul envoi, aucun repli texte. | setup/call/teardown réussis |
| `tests/unit/test_backfill_series_tomes.py::test_a_second_write_pass_adds_neither_book_link_nor_total` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_backfill_series_tomes.py::test_a_write_pass_links_and_prices_every_tome_of_the_series` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_bot_handlers.py::test_handle_select_edit_category` | Double incomplet | Une AsyncMock de commit ne persistait aucun jeton. Base SQLite isolée ; action relue par une autre session et bouton exact contrôlé. | setup/call/teardown réussis |
| `tests/unit/test_bot_handlers.py::test_handle_show_with_query` | Double incomplet | Une AsyncMock de commit ne persistait aucun jeton. Base SQLite isolée ; action relue par une autre session et bouton exact contrôlé. | setup/call/teardown réussis |
| `tests/unit/test_bot_handlers.py::test_paginate_alerts_with_category_slug` | Double incomplet | Une AsyncMock de commit ne persistait aucun jeton. Base SQLite isolée ; action relue par une autre session et bouton exact contrôlé. | setup/call/teardown réussis |
| `tests/unit/test_charte_copy.py::test_the_confirmation_is_the_recap_read_back_from_the_same_card` | Double incomplet | Identité MemoryStorage/StorageKey locale nécessaire au verrou du point 5. Confirmation : projection datée synthétique et libellé annuel du point 7 ; données et état FSM inchangés. | setup/call/teardown réussis |
| `tests/unit/test_night_mode.py::test_poller_night_mode_suspension` | Données synthétiques incomplètes | Activer le poller uniquement dans ce test dont polling, session et transports sont simulés ; sinon la boucle quitte avant la suspension nocturne. | setup/call/teardown réussis |
| `tests/unit/test_preflight_deploy.py::test_local_alembic_head_matches_the_repository` | Attente obsolète | Tête 0013, contrôlée indépendamment par ScriptDirectory, au lieu de 0010. | setup/call/teardown réussis |
| `tests/unit/test_requested_book_volume.py::test_stale_confirmation_does_not_write_an_unconfirmed_volume` | Double incomplet | Identité MemoryStorage/StorageKey locale nécessaire au verrou du point 5. Confirmation : projection datée synthétique et libellé annuel du point 7 ; données et état FSM inchangés. | setup/call/teardown réussis |
| `tests/unit/test_series_matching.py::test_counted_lot_resolves_to_the_whole_series_and_prices_it` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_series_matching.py::test_format_market_details_card_keeps_missing_tome_visible_in_lot` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_series_matching.py::test_market_details_card_series_tomes_formatting` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_series_matching.py::test_series_matching_chateau_des_animaux_lot_1_2_3` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_series_matching.py::test_series_matching_lot_card` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_series_matching.py::test_series_matching_lot_summary` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_series_matching.py::test_series_matching_lot_variations_matrix[lot_tomes0-2500-<b>Prix bas constat\xe9 (Lot) :</b> ~34,50 \u20ac-\u2705--9,50 \u20ac]` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_series_matching.py::test_series_matching_lot_variations_matrix[lot_tomes1-3000-<b>Prix bas constat\xe9 (Lot) :</b> ~36,00 \u20ac-\u2705--6,00 \u20ac]` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_series_matching.py::test_series_matching_lot_variations_matrix[lot_tomes2-6000-<b>Prix bas constat\xe9 (Lot) :</b> ~54,50 \u20ac-\u26d4\ufe0f-+5,50 \u20ac]` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_series_matching.py::test_series_matching_lot_variations_matrix[lot_tomes3-1500-<b>Prix bas constat\xe9 (Lot) :</b> ~18,50 \u20ac-\u2705--3,50 \u20ac]` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_series_matching.py::test_series_matching_specific_tome_summary` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_series_matching.py::test_single_volume_integrale_uses_its_prepared_price` | Attente obsolète / preuve synthétique incomplète | Point 7 : observations comparables datées, prix courant encore admissible ; agrégat non daté informatif. Minima et sommes gardent leurs montants exacts, dates distinctes explicites ; absence de preuve couverte négativement. | setup/call/teardown réussis |
| `tests/unit/test_unified_lifespan.py::test_cancelling_shutdown_does_not_recancel_the_poller_cleanup` | Prérequis manquant | Lancer seulement le scheduler dédié aux tâches factices ; arrêt et futurs terminés contrôlés. | setup/call/teardown réussis |
| `tests/unit/test_unified_lifespan.py::test_scheduler_cleanup_finishes_before_drain_and_client_closure` | Prérequis manquant | Lancer seulement le scheduler dédié aux tâches factices ; arrêt et futurs terminés contrôlés. | setup/call/teardown réussis |
| `tests/unit/test_unified_lifespan.py::test_shutdown_cancellation_still_drains_and_closes_clients[drain]` | Prérequis manquant | Lancer seulement le scheduler dédié aux tâches factices ; arrêt et futurs terminés contrôlés. | setup/call/teardown réussis |
| `tests/unit/test_unified_lifespan.py::test_shutdown_cancellation_still_drains_and_closes_clients[http]` | Prérequis manquant | Lancer seulement le scheduler dédié aux tâches factices ; arrêt et futurs terminés contrôlés. | setup/call/teardown réussis |

### Force des assertions et performance

Les lectures durables restent indépendantes : cartes avec propriétaire/alerte/version et jeton relus ; totaux après vrais snapshots/commits ; marqueurs de livraison et états unknown/abandoned relus. Les anciens agrégats financiers restent présents mais ne deviennent pas minima annuels. Dans le recalcul de série, des observations antérieures admissibles 300/450 sont persistées séparément des offres courantes 500/700 : les assertions de totals 300 puis 750 sont conservées. Le backfill garde exactement les anciens agrégats (somme 6500) et vérifie maintenant que le total admissible et la projection annuelle utilisent les observations courantes datées (somme 7500).

Les tests purs de rendu déclarent leur provenance synthétique dans la projection ; ils ne prouvent pas la persistance d’un historique réel. Les tests d’intégration utilisent les repositories réels et leurs commits. Trois nouveaux cas négatifs : minimum ancien sans date courante, courant périmé sans preuve annuelle, erreur réseau photo sans retry ni repli texte. Les scénarios négatifs existants de dates inconnues/périmées, ISBN invalide, ancienne décision et refus restent présents.

Les budgets de performance n’ont pas changé. Deux doubles d’envoi étaient refusés avant d’atteindre le travail prévu : la première mesure était invalide. Les six tests de performance passent avec le destinataire et le bot synthétiques complets. Le test de chronologie déterministe mesure davantage de travail qu’avant : six commits (ingestion, réservation 1, reçu 1, réservation 2, reçu 2, final). Avec son horloge factice, accusés à 600/1500 ms, persistance 300/300 ms, final 300 ms, total 2100 ms ; aucune modification de seuil pour absorber une latence réelle. Cela ne mesure pas les performances PostgreSQL/Telegram de production.

### Résultats complets finaux

| Sélection | Collectés | Réussis | Échoués | Non applicables | Désélectionnés |
|---|---:|---:|---:|---:|---:|
| Unitaires et performance, SQLite local selon fixture | 1442 | 1442 | 0 | 0 | 0 |
| Intégration PostgreSQL 16, sous-processus webhook SQLite selon contrat | 326 | 326 | 0 | 0 | 0 |

1768 nodeids distincts, dont les 1765 initiaux et trois nouveaux. `docs/consolidation-nodeids.json` conservait alors la référence exacte reconstruite à partir des rapports initiaux ; le retrait ciblé ultérieur est décrit en tête de ce document. Le lanceur refuse une collecte manquante ou dupliquée et une exécution incomplète, sans désélection ajoutée. Les identifiants de la matrice parametrée restent stables malgré le changement de libellé approuvé. Les rapports finaux n’ont aucun baseline manquant.

Commandes finales réellement exécutées :

```text
.venv/bin/python -B scripts/verify_consolidation.py unit
.venv/bin/python -B scripts/verify_consolidation.py postgres
.venv/bin/python -B scripts/verify_consolidation.py unit --target tests/integration/test_telegram_webhook_transient_failure.py
.venv/bin/python -B scripts/verify_consolidation.py typecheck
.venv/bin/ruff check .
bash -n scripts/backup_db.sh
```

Unitaires/performance : 26,25 s ; intégration : 60,95 s. Filet webhook ciblé : 12 parents, 62 scénarios enfants imposés et contrôles de rejet ; les deux autres parents webhook existants font partie des 326 tests d’intégration. Ne pas additionner ces sélections recouvrantes. Typage strict app + matching_diagnostics : 167 fichiers sans erreur ; quatre outils séparés avec follow-imports=silent : aucune erreur. Ruff dépôt et syntaxe Bash réussis. Deux avertissements de dépendances demeurent : Starlette TestClient/httpx et configuration Alembic sans path_separator ; aucune installation ni modification de configuration pour les masquer.

### Isolation et ressources

Dans chaque sélection finale : une sonde dotenv volontairement refusée, deux sondes réseau volontairement refusées, zéro tentative accidentelle bloquée et zéro service refusé. Les sous-processus Python gardés ne déclarent aucune tentative accidentelle. Pour unit, quatre schedulers dédiés autorisés exclusivement dans les scénarios de cycle de vie, nettoyage contrôlé. Pour PostgreSQL, connexions permises exclusivement à 127.0.0.1:55514 de l’instance jetable attestée.

Dernière instance : conteneur `3cba7907473accf810d007784223600715345cb8de47356be17d47fb1c1452d8`, system_id `7695791296614367272`, PostgreSQL 16, stockage tmpfs neuf ; sa suppression exacte a été attestée par le lanceur. Les autres instances ciblées et complètes de ce lot ont aussi été détruites dans leurs finally. Aucun Redis, bot, fournisseur ou application réel lancé. Aucun secret réel utilisé ou fichier .env lu.

Le lanceur PostgreSQL préexistant réexécute automatiquement installation vide et chaîne peuplée 0010→0011→0012→0013 avant la sélection : contrôles réussis ; ces réexécutions ne sont pas comptées comme des nodeids supplémentaires. Le backup/quarantaine n’a pas été rejoué dans ce lot : ses scripts, migrations et application n’ont pas changé, preuve synthétique du chapitre précédent conservée avec sa portée et sa référence. Aucune sauvegarde indépendante opérationnelle activée.

### État du plan et limites

Points 2 à 8, 10 et 11 : corrections locales précédentes conservées et sélections transversales pertinentes désormais vertes. Point 1 : partiel globalement, garanties limitées du parcours de pause conservées ; réception initiale et effets perdus en restauration non résolus. Point 5 : processus unique, pas crash/FSM/multiprocessus. Point 6 : reprise bornée, unknown jamais assimilé à absence d’envoi. Point 7 : référence documentée glissante, aucune reconstruction des observations purgées. Point 9 : outillage vérifié synthétiquement, pas sauvegarde réelle indépendante opérationnelle.

Aucune décision métier nouvelle nécessaire pour ce réalignement. Les prérequis d’exploitation précédents restent ouverts : destination/surveillance indépendantes, clés et responsables, configuration explicite, capacité mémoire des archives, traitement visible du refus de backup non configuré. Validation de services réels, ressources réelles et délai de restauration non établie. Aucun push, déploiement, activation ni garantie universelle exactement une fois.
