# Rafraîchissements, /add et cotation — préparation à la revue formelle

Date : 16 octobre 2026. Mandat : travail local réversible, sans service réel ni
opération Git de livraison. Base et HEAD : `5cbba4c9520459ce69decebbd2bf1be2b6cc950a`.
Branche : `fix/reliable-refresh-add`. Worktree :
`/Users/aminezegmou/Projets/bot-radar-worktrees/reliable-refresh-add`.
Le diff est non committé et l'index reste vide. La base distante n'a pas été
actualisée pendant ce mandat. La [revue précédente](reliability-review.md)
conserve les preuves historiques ; le présent document décrit la suite et ses limites.

Actualisation du 16 octobre 2026 : H1–H3 sont décidées. La revue distincte a ensuite
identifié trois défauts ; leur reprise locale et ses preuves figurent en section 10.
Les résultats de section 5 décrivent l'arbre antérieur à cette reprise.
Le réexamen ciblé consécutif est une auto-revue : son auteur a implémenté les
corrections. Deux défauts résiduels ont été traités sous le mandat de section 11 ;
aucun contrôle de cette reprise n'est qualifié d'indépendant.

## 1. Feuille de route priorisée

P0 : sûreté et absence de régression. P1 : preuves techniques manquantes.
P2 : préparation de la revue et livraison éventuelle. P3 : exploitation future.
Les statuts « hors mandat » signifient non exécuté, même si la recette est prête.

### Autonome maintenant

| ID / priorité | Objectif et zones | Dépendances | Critère de fin observable | Risques bornés | Statut |
| --- | --- | --- | --- | --- | --- |
| A1 / P0 | Vérifier branche, SHA, index, worktrees et sous-module | Aucune | Bon espace, index vide, sous-module épinglé ; aucun travail déplacé | Écriture dans un autre espace | fait |
| A2 / P0 | Relire tout le diff, les nouveaux tests et le rapport ; suivre les consommateurs | A1 | Cohérence résultats/compteurs/logs/cartes/contrats examinée | Une auto-revue peut manquer un défaut | fait |
| A3 / P0 | Corriger les défauts locaux dans création, cotation et bilan | A2 | Régressions rouges puis vertes ; critères courants rendus ; historique et tentatives non effectuées distingués | Réponses Telegram non transactionnelles | fait |
| A4 / P0 | Prouver la synthèse SQL dans `test_market_refresh_state` | A2 | Population BD comptée une fois ; jamais/ancien séparés ; prix informatif conservé après tentative non effectuée | SQLite ne valide pas PostgreSQL | fait |
| A5 / P0 | Ruff complet, typecheck isolé, suite isolée, puis reproduction scheduler | Dernier changement applicatif A3/A4 | Aucun échec nouveau ; trois erreurs préexistantes identifiées séparément | Le lanceur garde ses interdictions | fait |
| A6 / P1 | Matrice et recette PostgreSQL, section 3 | A2/A4, lecture du lanceur | Commandes, ressources, schéma destructif, assertions et arrêts explicités ; aucune exécution | Port fixe, image/daemon spécifiques, effacement du schéma de test | fait |
| A7 / P1 | Vérifications après déploiement, section 4 | A2 | Observations attendues, collecte de preuves et critères d'arrêt préparés | Ne pas provoquer d'incident en production | fait |
| A8 / P1 | Aligner `bd-preparation`, contrats catalogue/marché et Telegram/IA | A3 | Portée des 45 s et messages partiels exacts ; critères courants conservés | Ne pas transformer une limite en garantie | fait |
| A9 / P2 | Préparer commit et description de PR, sections 7/8 | A2/A5 | Texte centré sur le comportement final, tests et limites ; aucune création | Une description ne vaut pas autorisation Git | fait |
| A10 / P0 | Revue finale du diff, de l'index et du périmètre | A3–A9 | Diff propre, index vide, preuves et SHA consignés, aucun secret ni service réel utilisé | Contrôles devenus périmés si le code change | fait |

### Décisions humaines prises ; travaux restants

| ID / priorité | Question et zones | Dépendances | Options et recommandation | Critère de fin / conséquence | Risques | Statut |
| --- | --- | --- | --- | --- | --- | --- |
| H1 / P1 | Conflit entre garde et tests scheduler | A5 | Décision : réparation ciblée sous mandat distinct, transports réels toujours interdits ; aucun contournement | Décision acquise ; réparation et vérification restent hors de ce lot | Mélanger harness et correctif produit | fait |
| H2 / P1 | Portée des 45 secondes | A2/A8 | Décision : borne du bloc initial uniquement, aucune promesse de durée totale | Le relecteur doit encore examiner délais de persistance, récupération après échec et absence d'activation indue | Persistance ultérieure non bornée par ces 45 s | fait |
| H3 / P2 | Examen distinct avec preuves manquantes | A5/A6, H1/H2 | Décision : examen possible avec les trois échecs scheduler et les limites PostgreSQL, CI, fournisseurs et production | Examen distinct effectué par agents distincts ; trois constats à corriger puis à réexaminer ; aucune approbation humaine ou validation CI/SQL impliquée | Confondre avis de code, revue humaine et preuve technique | fait |

Le dédoublonnage d'une heure, le repli Redis local, les filtres de marché et la
concurrence sont les choix déjà retenus ; ils ne sont pas rouverts ici. Le choix
H2 ne promet aucune durée totale de commande.

### Nécessite un mandat distinct

| ID / priorité | Objectif et zones | Dépendances | Critère de fin observable | Risques / portée à autoriser | Statut |
| --- | --- | --- | --- | --- | --- |
| M1 / P1 | Exécuter la recette PostgreSQL ciblée puis les contrôles requis | A6, ressources disponibles | `backend: postgres`, test concurrent effectivement exécuté, assertions vertes, ressource retirée | Créer/démarrer/migrer/effacer/supprimer uniquement le conteneur neuf identifié | hors mandat |
| M2 / P1 | Diagnostiquer le « 23/23 », vérifier image et logs expurgés | Traces et identifiant d'image accessibles | Établir une cause par source/opération, ou garder « indéterminée » | Accès réel aux logs/images ; aucune lecture de secrets, aucun appel fournisseur implicite | hors mandat |
| M3 / P2 | Vérifier `origin/main`, l'écart de base et la nécessité d'une intégration | Réseau Git autorisé | Base distante datée et comparaison ; toute intégration/rebase reste séparément autorisée | Modifier la base invalide les preuves pertinentes | hors mandat |
| M4 / P2 | Créer un commit local | A5/A10, décision humaine sur les limites | Index explicitement relu, commit du seul périmètre, SHA consigné | Le projet de message ne vaut pas mandat | hors mandat |
| M5 / P2 | Pousser la branche | M4 | SHA distant conforme, sans force-push | Écriture distante et CI déclenchée | hors mandat |
| M6 / P2 | Créer la PR vers `main`, attacher la PR et contrôler la CI | M5, projet section 8 | PR réellement créée seulement sur mandat ; `check` et `test-postgres` observés ; relecteur identifié | Aucun succès CI déduit des résultats locaux | hors mandat |
| M7 / P3 | Fusionner la PR exacte | H1–H3, M1/M3/M6, CI verte et branche à jour | Présenter SHA/risques/tests puis obtenir l'accord explicite sur cette PR et ce commit | Aucun contournement de protection ; aucun merge impliqué par la revue | hors mandat |
| M8 / P3 | Déployer une image identifiée | M7 et mandat explicite de déploiement | Procédure canonique suivie, SHA/digest enregistrés et retour arrière préparé | Fusion ≠ déploiement ; migrations ou actions de service restent explicitement bornées | hors mandat |
| M9 / P3 | Effectuer les vérifications réelles de section 4 | M8 et mandat d'accès/validation réel | Résultats datés et corrélés à l'image ; aucune observation inventée | Autoriser séparément les éventuelles alertes de test et leur nettoyage | hors mandat |

### Post-déploiement

| ID / priorité | Objectif et zones | Dépendances | Critère de fin observable | Risques | Statut |
| --- | --- | --- | --- | --- | --- |
| P1 / P3 | Identifier l'image et vérifier processus, démarrage et scheduler unique | M8/M9 | SHA/digest exécutés, santé et configuration de jobs observés | Ne pas déduire l'image d'un merge | hors mandat |
| P2 / P3 | Observer une occurrence complète, y compris population et fraîcheur | P1 | Un seul bilan courant par occurrence, aucun détail stale séparé, compteurs corrélés | TTL et remise Telegram ambiguë limitent l'unicité | hors mandat |
| P3 / P3 | Observer incrémental, nouvel incident, aggravation et rétablissement | P1, événement réel ou environnement de validation autorisé | État inchangé silencieux ; source/opération réellement interrogée pour rétablissement | Ne pas provoquer une panne en production ; si aucun incident, scénario non observé | hors mandat |
| P4 / P3 | Vérifier `/add`, remplacement confirmé, `/skip`, exclusions et carte enrichie | P1, compte et alertes de validation autorisés | Identité et critères cohérents, aucune vieille réponse appliquée, même carte enrichie | Créations/suppressions de tests et appels externes exigent leur périmètre | hors mandat |
| P5 / P3 | Comparer cache, relevé courant et historique aux observations réelles | P1 et consultation fournisseur autorisée | Édition/ISBN/état/port/date/provenance vérifiés ; aucun rajeunissement après échec | 20,59 € et 8,99 € ne sont pas des constantes ni des prix validés | hors mandat |
| P6 / P3 | Clore la vérification et traiter les écarts observés | P2–P5 | Rapport des scénarios observés/non observés ; retour arrière seulement sur mandat | Pas de modification DB, concurrence, retries ou services improvisée | hors mandat |

## 2. Corrections et preuves locales de ce mandat

- `alerts_create._search_keywords_in_background` et `handle_create_ai_variants`
  reconstruisaient le récap à partir de l'instantané avant attente IA. Une modification
  des conditions ne change pas la révision bibliographique : le résultat pouvait
  donc afficher les anciennes conditions malgré un FSM correct. La garde relit
  maintenant le brouillon, contrôle l'identité, puis utilise les critères courants.
  Aucun réseau n'a été ajouté sous verrou.
- `market_price_service.get_comparison_summary` attribuait une indisponibilité
  aux sources pour tout résultat partiel, y compris un échec historique seul.
  Le texte dit désormais que certaines données n'ont pas été actualisées ; le
  prix courant reste affichable et le diagnostic détaillé reste disponible.
- Trois scénarios ont échoué avant correction : deux parcours de variantes,
  automatique et bouton, et le texte de cotation partielle. Après correction,
  110 tests des consommateurs passent en 0,49 s.
- Une régression SQL dans `test_market_refresh_state` vérifie la sélection vide,
  le livre partagé actif/en pause, les livres jamais collectés malgré un ancien
  prix, l'ancien succès et les exclusions de catégorie/statut/suppression/liaison.
  Avec préparation, références annuelles et notifications : 77 réussites en 2,41 s,
  sur SQLite uniquement. Le même test accepte le futur backend PostgreSQL.
- Le parcours de test « IA désactivée » était neutralisé par le double partagé
  `tests.add_journey.patched`, qui forçait `is_enabled=True`. Une assertion d'absence
  d'appel IA a reproduit trois faux parcours. Le double suit désormais le mode
  paramétré et les cas sans IA passent par `handle_category_selected`, puis la
  résolution réelle du handler avec catalogue simulé. Les 71 tests ciblés passent
  en 0,43 s. Le nombre de cas est inchangé ; la garantie est désormais réellement éprouvée.
- Une tentative où toutes les sources sont `skipped` était persistée `empty` et
  pouvait effacer un ancien prix informatif non comparable. Elle conserve désormais
  l'état `skipped`, les prix/offres/dates antérieurs et affiche « Aucune source
  interrogée », sans succès, panne ou absence d'offre inventés. Le bilan compte les
  tentatives non effectuées ; un incident n'est pas déclaré rétabli par ces tentatives.
  Deux scénarios de rendu et un test de persistance ont reproduit le défaut avant
  correction. L'état utilise la colonne texte existante, sans migration.
- Un avertissement SQLite a été localisé par une sonde temporaire dans les tâches
  de fond de deux tests de panneau : elles sortaient des doubles du test. La vraie
  frontière `_refresh_panel` est maintenant simulée, ses tâches sont attendues et
  les assertions de rendu/boutons sont conservées. Une première tentative de doubler
  `view_market_details.price_book_now` ne ciblait pas la bonne frontière ; elle a
  échoué et a été corrigée. Aucun changement applicatif du panneau n'a été ajouté.
  Les 123 tests ciblés passent ensuite en 1,06 s, sans avertissement. La dernière
  sélection cotation/SQL/notifications donne 69 réussites en 0,70 s.
- Des lignes trop longues ont été corrigées ; aucun test n'est supprimé,
  désactivé ou assoupli. Les preuves historiques restent dans le rapport précédent.

## 3. Recette PostgreSQL prête, non exécutée

Point d'entrée existant : `scripts/verify_consolidation.py postgres`. Ne pas lancer
`pytest` directement avec une URL de base : `tests.database` efface et recrée le
schéma, et une configuration factice seule ne bloque pas les transports réels.

Avant toute exécution, obtenir un mandat M1 couvrant : accès au daemon Docker local,
création d'un conteneur PostgreSQL neuf, migrations et effacement de son seul schéma
synthétique, connexion à cette seule ressource et suppression de cette ressource.
Ne pas réutiliser un PostgreSQL existant ou une base personnelle.

Préconditions à vérifier seulement sous ce mandat :

1. Relire les deux scripts de recette et leur version ; confirmer le worktree.
2. Vérifier la disponibilité du port fixé `127.0.0.1:55514` sans arrêter un service.
   S'il est occupé, s'arrêter : aucun remplacement de service ni nouveau port implicite.
3. Le daemon configuré est `unix:///Users/aminezegmou/.docker/run/docker.sock`.
   L'image épinglée est `sha256:c05eced0bdb41ea9b95a656472a6aa4d50cad0d8a2e33d14eb1c53fd6204f2ae`.
   Le lanceur impose `--pull=never` ; si elle manque, s'arrêter. Aucun téléchargement
   ou changement d'image n'est autorisé implicitement.
4. Le lanceur crée une configuration Docker temporaire, un label aléatoire et
   un stockage tmpfs, sans montage d'un volume existant. Il atteste l'identifiant
   système PostgreSQL depuis le conteneur neuf. Ne pas fournir `.env`, URL réelle,
   session, Redis ou identifiants personnels.
5. Le fils neutralise l'environnement et n'autorise que l'endpoint PostgreSQL
   de cette recette ; HTTP, Redis et secrets réels restent exclus. Les migrations
   existantes sont exercées même avec une sélection `--target` ciblée.

Commande proposée, **non exécutée** :

```sh
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py postgres \
  --target tests/integration/test_market_refresh_state.py \
  --target tests/integration/test_bd_preparation.py \
  --target tests/integration/test_annual_market_reference.py \
  --target tests/integration/test_series_pricing_concurrency.py \
  --target tests/integration/test_isbn_removal.py \
  --target tests/integration/test_alert_market_panel.py
```

| Preuve nécessaire | Tests préparés/existants | Critère PostgreSQL |
| --- | --- | --- |
| Agrégats de fraîcheur, unicité de population et absence de rajeunissement | `test_market_refresh_state.py` | Requête `FILTER` exécutée sur PostgreSQL ; valeurs et dates relues dans une autre session selon les tests |
| Préparation durable, annulation, remplacement et échec de persistance | `test_bd_preparation.py` | Ancienne version incapable de rattacher/activer ; écritures et reprise cohérentes avec les vrais verrous |
| Observations datées, rollback, projection annuelle | `test_annual_market_reference.py` | Montant, date et provenance préservés ; aucune observation réinventée par reprise |
| Concurrence réelle des écritures de cotation | `test_series_pricing_concurrency.py::test_two_committing_tomes_leave_the_complete_series_total` | Test exécuté, non ignoré, dialecte PostgreSQL attesté et total final complet |
| Retrait d'ISBN, liaisons et atomicité après échec | `test_isbn_removal.py` | Ancienne identité/liaisons préservées si commit échoue ; livre partagé conservé |
| Carte après écriture dans une session distincte | `test_alert_market_panel.py::test_creation_reloads_a_cache_written_in_another_session` | Carte relit la cote durable et non un objet du collecteur |

Conserver `FINAL_RESOURCE`, `FINAL_EMPTY_INSTALL`, les preuves des migrations,
`FINAL_SUITE_REPORT` et `FINAL_RESOURCE_REMOVED`. Vérifier backend, nodeids,
assertions, sorties et absence d'accès interdit dans le parent et les enfants.
Le nettoyage automatique ne doit viser que l'ID créé par cette invocation.
Le budget du fils est de 240 s ; un dépassement n'est pas un succès partiel.
Si nettoyage échoue, conserver l'ID et s'arrêter, sans prune global.

Cette sélection ne remplace pas la CI complète `test-postgres`. Après H1 et un
mandat couvrant le rejeu complet, la même recette peut être lancée sans `--target`.
Les trois tests scheduler restent en échec sous le garde actuel, quel que soit
le backend SQL ; ne pas présenter la suite complète comme verte avant résolution.

## 4. Vérifications après déploiement prêtes, non exécutées

Ces vérifications exigent M8/M9 ; elles ne découlent pas d'une autorisation de merge.
Utiliser les procédures canoniques de [déploiement](operations/deployment.md),
[préparation](bd-preparation.md), [marché](contracts/catalogue-market.md) et
[Telegram/IA](contracts/telegram-ai.md), sans lancement de script sensible implicite.

1. Relever SHA et digest de l'image réellement exécutée, heure UTC, santé et
   processus unique ; vérifier `max_instances=1` et `coalesce=True`. Ne pas redémarrer
   ou modifier un job pour produire artificiellement une preuve.
2. Observer une passe complète naturelle. Corréler occurrence, livres sélectionnés,
   traités/non parcourus, relevés courants, caches réutilisés, absences, dégradations
   et exceptions. Compter les messages par occurrence et destinataire. Attendre
   un seul bilan courant, sans longue liste stale et sans promesse de reprise.
3. Pour chaque source interrogée, relever opération, catégorie, statut HTTP,
   durée du connecteur et livre/occurrence. Un résultat vide valide est une réussite
   sans offre. L'historique n'est ni une offre courante ni une panne courante.
4. Observer l'incrémental : incident nouveau, aggravation, silence à état inchangé,
   puis rétablissement sur une sollicitation réelle de la même source/opération.
   Sans événement observable, marquer « non observé » ; ne pas provoquer de panne,
   enlever des secrets ou modifier les services pour satisfaire cette étape.
5. Sur le compte et les alertes de validation explicitement autorisés, vérifier
   les cinq saisies fournies de Jours de sable, le placeholder, le conflit ISBN,
   « Conserver »/« Remplacer », `/skip`, annulation/remplacement pendant recherche
   et modification des conditions pendant variantes. Vérifier titre canonique,
   autrice/éditeur, critères, absence d'exclusions contradictoires et même carte
   après préparation. Nettoyer seulement les alertes de test autorisées.
6. Comparer cache frais puis collecte bornée ; vérifier prix courant sans historique,
   collecte vide, échec transitoire et prix conservé avec sa date réelle. Une source
   doit être effectivement appelée avant toute conclusion de rétablissement.
7. Pour 20,59 € ou 8,99 €, relever URL, ISBN/édition, disponibilité, état, livraison,
   devise, date UTC et nature/période de l'historique. Ne pas exiger ces montants ni
   qualifier de minimum annuel exhaustif un petit historique local.
8. En cas de mauvais ISBN, activation d'une ancienne version, prix rajeuni sans
   observation, incidents masqués ou messages répétés hors limites annoncées,
   conserver les traces expurgées et solliciter l'action opérationnelle autorisée.
   Ne pas modifier DB/Redis, concurrence, retries ou image de sa propre initiative.

Pour le « 23/23 » historique : demander à l'opérateur les traces expurgées autour de
l'occurrence, le SHA/digest exécuté, le résultat de sélection et les causes par
source/opération. Sans ces éléments, la cause rétroactive reste indéterminée.
Les PR #121/#122 fusionnées ne prouvent pas quelle image était exécutée.

## 5. Contrôles finaux locaux

Résultats du mandat autonome : **autonome terminé**. La branche reste non committée
et ne dispose ni de preuve PostgreSQL ni d’une CI vérifiée. La suite antérieure
comptait 3 échecs, 2088 réussites, 12 ignorés et 52 régressions vertes.
Les commandes ne créent ni commit ni PR et utilisent la recette d'isolation existante.
Elles sont mesurées avec `/usr/bin/time -p` ; les durées pytest et murales sont
distinguées dans les résultats.

```sh
/Users/aminezegmou/Projets/bot-radar/.venv/bin/ruff check .
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py typecheck
make test-isolated PYTHON=/Users/aminezegmou/Projets/bot-radar/.venv/bin/python
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py unit --target tests/integration/test_scheduler_occurrence.py
```

| Contrôle, dans l'ordre ci-dessus | Résultat exact | Durée |
| --- | --- | --- |
| Ruff complet | `All checks passed!`, sortie 0 | 0,03 s murales |
| Typecheck isolé | Aucune erreur sur 162 fichiers puis 4 fichiers ; sortie 0 | 20,44 s murales |
| Suite isolée complète | **3 failed, 2095 passed, 12 skipped, 4 warnings**, pytest 1 / make 2 | 77,86 s pytest ; 81,66 s murales |
| Reproduction scheduler seule | **3 failed**, sortie 1 | 0,15 s pytest ; 2,87 s murales |

Différence avec l'état initial : **7 réussites supplémentaires**, aucun échec nouveau,
mêmes 12 tests ignorés et mêmes quatre avertissements de dépréciation/configuration.
Les **59 régressions ajoutées au total passent**, y compris les cinq cas `/add`
avec IA réellement activée ou désactivée. Le nombre de cas IA n'a pas été gonflé :
leur câblage et leurs assertions ont été corrigés.

Les trois échecs sont exactement :

- `tests/integration/test_scheduler_occurrence.py::test_the_real_scheduler_hands_the_intended_run_time_to_the_job`
- `tests/integration/test_scheduler_occurrence.py::test_the_transmitted_time_is_the_triggered_run_not_the_next_one`
- `tests/integration/test_scheduler_occurrence.py::test_a_delayed_run_is_still_identified_by_its_own_occurrence`

Le garde refuse le démarrage avec `Scheduler réel interdit`, puis le nettoyage
lève `SchedulerNotRunningError`. Identifiants et causes correspondent à la preuve
historique sur main propre à `5cbba4c` (3 échecs en 0,16 s). Le scheduler, ses tests
et le lanceur n'ont pas été modifiés ; le garde n'a pas été contourné.

Les rapports `FINAL_SUITE_REPORT` confirment 2110 tests collectés, les 59 scénarios
réussis et `allowed: 0` dans le parent et les enfants. Les sondes d'auto-vérification
du lanceur ont été refusées ; aucun service réel n'a été utilisé. Les essais
PostgreSQL et post-déploiement restent **non exécutés**.

Sorties locales temporaires : `/tmp/radar-roadmap-ruff.txt`,
`/tmp/radar-roadmap-typecheck.txt`, `/tmp/radar-roadmap-tests.txt`,
`/tmp/radar-roadmap-scheduler.txt`. Les sorties de phase rouge et les sondes SQLite
restent sous `/tmp/radar-roadmap-*` et `/tmp/radar-connection-origin.txt` ; elles ne
font pas partie des contrôles finaux ni du diff proposé.

Contrôle final : `git diff --check` propre, index vide, HEAD inchangé à `5cbba4c`.
Dossier principal propre. Liens locaux des documents modifiés vérifiés. Aucun
commit, push, PR, merge, rebase, déploiement, migration ou service réel n'a été utilisé.

## 6. Limites et préparation à la revue formelle

Le mandat autonome peut être terminé sans rendre la branche fusionnable.
La proposition en revue formelle doit annoncer H1/H2 et l'absence de preuve M1 ;
une revue de code peut commencer avec ces réserves, mais la validation PostgreSQL
et la CI requise ne sont pas réputées acquises. H3 autorise cet examen avec limites ;
les corrections consécutives à l'examen distinct doivent être réexaminées.

Le dédoublonnage par occurrence est borné à une heure. Redis utilise un repli local
qui ne survit pas au redémarrage ; sa remise n'est pas transactionnelle avec Telegram.
Un acquittement ambigu ou une remise partielle peut produire un doublon. Le signal
« rétablissement » décrit les tentatives observées, pas la santé universelle du site.
Les durées portent sur l'appel connecteur, pas chaque requête HTTP séparée.

Une cotation partagée peut finir dans sa session après l'arrêt de la préparation,
sans réactiver une ancienne identité. Le délai de 45 s ne borne pas l'écriture
ultérieure de l'échec. La protection des brouillons est dans le processus unique ;
elle ne transforme pas l'édition Telegram en transaction atomique avec le FSM.

Aucune réparation réelle de fournisseur, prix manuel, version déployée, performance
PostgreSQL ou couverture annuelle exhaustive n'est attestée. Aucun nettoyage massif
des alertes existantes n'est proposé. L'auto-revue reste une auto-revue.

## 7. Projet de message de commit — non créé

```text
fix: fiabiliser les rafraîchissements, /add et la cotation initiale

Consolider les bilans par occurrence et suivre les incidents par source et
opération sans compter les caches ou les anciens prix comme des actualisations.

Résoudre les ISBN saisis avant l'IA, réaligner l'identité canonique avec
confirmation des conflits et protéger les brouillons des réponses tardives.
Valider les exclusions fusionnées avec l'autrice et l'éditeur confirmés.

Réutiliser le cache valable et borner le bloc initial de préparation ;
conserver les observations après échec avec leur date réelle.

Ajouter les régressions et documenter les limites du dédoublonnage, du délai,
de la validation SQLite et les recettes PostgreSQL/post-déploiement non exécutées.
```

## 8. Projet de description de PR — non créée

Titre : **Fiabiliser les bilans de rafraîchissement, /add et la cotation initiale**

Les rafraîchissements pouvaient envoyer plusieurs messages pour une même passe,
les diagnostics ne distinguaient pas correctement panne, absence d'offre et
historique, et un ISBN saisi pouvait être refusé face à un titre artificiel.
Le correctif consolide le bilan et conserve l'identité bibliographique confirmée
jusqu'aux critères et à la carte, y compris après remplacement ou réponse tardive.

- Résultats de tentative structurés et incidents par source/opération ; caches,
  observations anciennes, tentatives non effectuées, absences normales et exceptions restent distincts.
- ISBN explicite avant IA, choix court d'œuvre, confirmation des conflits,
  `/skip` visible, critères courants et exclusions fusionnées cohérents.
- Cache valable réutilisé, bloc initial de préparation borné à 45 s et reprise
  transitoire limitée ; prix courant affichable sans historique et dates conservées.
- Aucun changement des formules, de la concurrence ou du chemin ingestion vers Telegram.

Validation locale sur la base `5cbba4c`, avec configuration factice et transports
réels refusés :

- `ruff check .` : réussi.
- `python -B scripts/verify_consolidation.py typecheck` : réussi, 162 puis 4 fichiers.
- `make test-isolated PYTHON=/Users/aminezegmou/Projets/bot-radar/.venv/bin/python` :
  3 failed, 2095 passed, 12 skipped, 4 warnings en 77,86 s ; 59 régressions ajoutées vertes.
- Reproduction séparée `python -B scripts/verify_consolidation.py unit --target
  tests/integration/test_scheduler_occurrence.py` : les mêmes 3 échecs en 0,15 s.

Ces échecs préexistent sur main à la même base : les tests d'horaire transmis,
d'occurrence déclenchée et d'exécution retardée sont refusés par le garde scheduler,
puis lèvent `SchedulerNotRunningError` au nettoyage. Aucun test n'est désactivé.
PostgreSQL, CI, fournisseurs réels et production ne sont pas validés ; recettes
et critères sont préparés seulement dans `docs/reliability-roadmap.md`.

Risques : remise Telegram/Redis non transactionnelle, dédoublonnage borné,
écriture d'échec hors budget initial et preuve de concurrence PostgreSQL manquante.
La revue préparatoire est une auto-revue. Cette description ne demande ni merge
ni déploiement et ne revendique aucune réparation du « 23/23 » historique.

## 9. Prochaines actions humaines minimales

1. Faire réexaminer les trois corrections et leurs preuves ; H1–H3 sont décidées.
2. Autoriser M1 avec ses ressources, migrations synthétiques et nettoyage bornés,
   puis obtenir les preuves PostgreSQL sur le contenu final.
3. Désigner le relecteur indépendant et lui transmettre diff, résultats et limites.
4. Seulement si souhaité, mandater séparément commit, push et création de PR ;
   vérifier ensuite les deux jobs CI et l'état de la base distante.
5. Merge, déploiement et vérifications réelles restent des mandats ultérieurs.

## 10. Reprise ciblée après examen distinct — 16 octobre 2026

Cette reprise n'étend pas le mandat autonome antérieur. Elle traite uniquement
les trois constats et leurs conséquences directes. Les causes, les reproductions
et les corrections sont détaillées dans la [section 9 du rapport](reliability-review.md#9-reprise-locale-des-trois-constats-de-lexamen-distinct).

| Étape | Catégorie | Critère et preuve | Statut |
| --- | --- | --- | --- |
| Callback de catégorie ancien | Locale autorisée | Vrai middleware ; A suspendu, nouvelle création B ; données et état de B inchangés ; recherches appelées protégées après attente | fait |
| Observation et notification des incidents | Locale autorisée | `3/3 → 1/3 → 3/3`, relecture persistée, repli local, échec de remise puis reprise, aucun faux rétablissement | fait |
| Mode bibliographique durable après `/skip` | Locale autorisée | Notice unitaire sans numéro rattachée et cotée ; reprise sans FSM ; modes série et inconnu préservés ; remplacement invalide l'ancienne version | fait |
| Contrôles complets après corrections | Locale autorisée | Ruff, typecheck, suite isolée puis reproduction scheduler, résultats ci-dessous | fait |
| Réexamen des trois corrections | Examen distinct à prévoir | Relire code et preuves sur le contenu final ; aucune approbation déduite des tests locaux | à faire |
| Réparation du dispositif scheduler H1 | Mandat distinct | Transports interdits, garde conservé, trois scénarios réellement exercés | hors mandat |
| PostgreSQL, CI, réseau réel et livraison | Mandats distincts existants | M1–M9 inchangés ; aucun de ces accès exécuté dans cette reprise | hors mandat |

Impact sur la recette PostgreSQL : aucune commande, ressource ou migration
supplémentaire. La cible existante `tests/integration/test_bd_preparation.py`
inclut désormais les sept nouveaux scénarios de mode durable, reprise et version
périmée. Ils doivent être rejoués sur PostgreSQL ; leur réussite SQLite ne prouve
pas les verrous. Vérifier aussi le résultat en session distincte et les délais de
persistance exigés par H2. La recette et les vérifications post-déploiement de
sections 3/4 restent préparées, non exécutées.

Le projet de PR de section 8 décrit l'arbre antérieur ; avant toute publication
autorisée, lui joindre les présentes corrections et les résultats actualisés.
Ce document ne crée ni commit ni PR et n'autorise aucune publication.

Résultats finaux : Ruff réussi (0,06 s murales), typecheck réussi sur 162 puis
4 fichiers (17,51 s murales), suite isolée **3 failed, 2109 passed, 12 skipped,
4 warnings en 91,43 s** (95,78 s murales), puis scheduler seul **3 failed en
0,20 s** (3,45 s murales). Même signature et même cause des trois échecs scheduler ;
14 réussites supplémentaires sur l'arbre précédent. Les commandes exactes,
incidents de cache et limites sont conservés dans la section 9 du rapport.
Les quatorze nouveaux scénarios passent. Les constats restent à faire réexaminer ;
aucune validation PostgreSQL ou CI n'est déduite de ces résultats locaux.

## 11. Deux défauts résiduels — reprise locale après auto-revue ciblée

Périmètre : génération/phase du clic de catégorie au sein d'un même brouillon et
synthèse d'incident non remis puis rétabli. Voir la [section 10 du rapport](reliability-review.md#10-correction-des-deux-défauts-résiduels-après-auto-revue-ciblée)
pour les causes, les reproductions et les preuves. Il ne s'agit pas d'une nouvelle
revue générale ni d'une revue indépendante.

Les cinq reproductions initiales échouaient ; après correction, les 112 scénarios
ciblés passent. Six scénarios nouveaux sont ajoutés au total. Les trois vues des
incidents restent distinctes, dans le stockage existant ; les confirmations de
remise ne constituent pas une garantie exactement une fois.

Les rapports des sections précédentes restent des preuves historiques. One-shot
après `/skip` reste clos sur examen statique ; PostgreSQL reste non exécuté. H1–H3,
la recette PostgreSQL, les limites H2, la CI et la validation réelle ne sont pas
rouverts. Aucun changement de schéma ni de ressource de validation nécessaire.

Cette reprise constituait une auto-revue ciblée. Le réexamen distinct ultérieur
clôt les deux constats sur examen statique ; le complément de couverture demandé
ensuite est décrit en section 12.

Contrôles finaux terminés dans l'ordre requis : Ruff réussi en 0,08 s murales ;
typecheck réussi sur 162 puis 4 fichiers en 16,57 s murales ; suite isolée
**3 failed, 2115 passed, 12 skipped, 4 warnings en 74,24 s** (77,60 s murales), puis
scheduler seul **3 failed en 0,16 s** (2,59 s murales). Aucun échec nouveau ; mêmes
nodeids et causes scheduler, sans contournement. Six réussites supplémentaires
par rapport à la reprise précédente. Commandes et sorties sont en section 10 du
rapport. Diff non committé, index vide et HEAD inchangé ; aucun service réel.
Ces résultats historiques restent ceux de l'auteur avant le réexamen distinct ;
ils ne constituent aucune autorisation de publication ou validation de fusion.

## 12. Relecture persistée après nouvelle panne — complément du 16 octobre 2026

Les deux constats sont **clos sur examen statique par le relecteur distinct**, qui
n'avait pas participé à leur implémentation. Aucun défaut applicatif résiduel n'est
confirmé dans ce périmètre. Cette conclusion reste distincte de la preuve exécutée
par l'auteur du complément de test.

Le nouveau scénario utilise un stockage Redis simulé restituant les chaînes JSON
écrites. Il vérifie l'incident non remis, le rétablissement et sa synthèse persistée,
une reprise suivie d'une nouvelle panne non remise, une seconde reprise avec remise
réussie du passé et de l'incident courant, puis le silence à état inchangé après
relecture. Les trois vues, le message et les appels de lecture sont vérifiés ; les
états locaux et les doubles sont remplacés à chaque reprise. Seul le JSON persiste.

Le test passe dès son premier lancement, sans correction applicative ni phase rouge :
**couverture supplémentaire**. Après une correction de longueur de ligne dans le
test, son lancement ciblé final donne **1 passed en 0,10 s** (2,68 s murales).

Contrôles finaux dans l'ordre demandé : Ruff complet réussi (0,04 s murales),
typecheck isolé réussi sur 162 puis 4 fichiers (16,61 s murales), suite isolée
**3 failed, 2116 passed, 12 skipped, 4 warnings en 71,72 s** (75,10 s murales), puis
scheduler séparé **3 failed en 0,16 s** (2,47 s murales). Les trois seuls échecs
conservent les nodeids et la signature scheduler antérieurs ; aucun contournement.
Une première tentative de typecheck avait échoué avant analyse sur le cache mypy
inaccessible sous la sandbox ; la même commande a été reprise avec l'écriture locale
autorisée. Commandes, sorties, incidents d'outillage et portée figurent en
[section 11 du rapport](reliability-review.md#11-réexamen-distinct-clos-et-complément-de-couverture--16-octobre-2026).

État du complément : **terminé**. Validation complète de branche : **partielle** à
cause des trois échecs scheduler conservés. PostgreSQL, CI, fournisseurs et production
restent non validés ; aucune garantie exactement une fois pour Telegram/Redis.
Ce complément exécuté par son auteur ne constitue pas une nouvelle revue distincte,
une validation humaine ou une autorisation de publication, fusion ou déploiement.

Diff conservé non committé, index vide, branche `fix/reliable-refresh-add`, HEAD
`5cbba4c9520459ce69decebbd2bf1be2b6cc950a` inchangé. Seuls le test et les deux
rapports changent dans cette reprise ; aucun fichier applicatif ni autre worktree
modifié, aucun service réel, secret ou opération de livraison.

## Validation PostgreSQL et complément 0013→0014 — 17 octobre 2026

Les paragraphes précédents décrivent leurs états historiques. H1 est désormais
intégré localement : les trois tests scheduler passent, et la suite combinée avant
ce complément donnait 2 129 réussites, 12 ignorés et aucun échec. Ils ne sont plus
des échecs préexistants acceptés.

La recette PostgreSQL ciblée a été exécutée sous mandat distinct, puis complétée
pour prouver la migration peuplée 0013→0014 et lire le vrai marqueur d'installation.
Le rejeu final donne 106 tests réussis, aucun ignoré, et une preuve PostgreSQL de
30 cas de backfill, conservation des anciennes données/contraintes et validation
des nouveaux champs. Les ressources synthétiques sont supprimées et le port est
disponible. Voir la [preuve détaillée](migration-0014-validation.md).

Après ce complément d'outillage, Ruff et les typechecks passent, ainsi que les
58 scénarios ciblés lanceur/scheduler. La suite isolée complète dépasse son plafond
inchangé de 240 s sans bilan final : son rejeu réussi reste nécessaire avant
livraison. Elle n'est pas déclarée verte à partir des résultats antérieurs.

La CI et la mise à jour de base restent à traiter sous leurs mandats. Cette preuve
ciblée ne valide ni la production ni les fournisseurs, et ne constitue pas une
revue indépendante. H2 reste inchangé : les 45 secondes bornent le bloc initial,
sans durée totale promise. Aucun commit, push, PR, merge ou déploiement effectué.

### Rejeu complet après diagnostic local du plafond — 17 octobre 2026

Le [diagnostic ciblé](isolated-suite-timeout-diagnostic.md) conserve le timeout et
le `F` historique non expliqué, ajoute des mesures optionnelles hors capture,
puis obtient deux suites complètes réussies. Résultat final : **2 133 passed,
12 skipped, 4 warnings, aucun échec**, en 78,17 s de durée murale, avec Ruff et
typecheck réussis. Le plafond reste 240 s et les gardes sont inchangés.

Le blocage de rejeu local est levé ; la cause historique du ralentissement n'est
pas revendiquée comme réparée. Aucun code applicatif, migration, PostgreSQL,
service réel, base Git ou opération de livraison modifié/exécuté dans ce diagnostic.
CI et intégration de base restent sous leurs mandats distincts.

### Intégration autorisée de main — 17 octobre 2026

Le [rapport d'intégration](reliability-main-integration.md) décrit le merge de
`034f48e` (#124–#126), la réconciliation du seul garde d'occurrence et la conservation
des garanties des deux parents. Les contrôles de l'arbre intégré passent : Ruff,
typecheck, 2 182 réussites en suite complète et 106 réussites PostgreSQL ciblées,
avec concurrence réelle et preuve peuplée 0013→0014. La ressource est supprimée.
La CI doit être observée sur le nouveau SHA après push ; aucune fusion dans main
ni aucun déploiement n'est couvert par ce mandat.
