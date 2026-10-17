# Preuve locale de migration 0013→0014

Date : 17 octobre 2026. Branche `fix/reliable-refresh-add`, HEAD local inchangé
`5cbba4c9520459ce69decebbd2bf1be2b6cc950a`, correctif non committé et index vide.

## Périmètre et dispositif

La validation PostgreSQL ciblée précédente exécutait l'installation à vide jusqu'à
0014, mais son marqueur affichait 0013 et le scénario peuplé s'arrêtait à 0013.
Le lanceur lit maintenant `alembic_version`, exige la tête Alembic attendue et
affiche la valeur réellement lue. Trois tests vérifient la révision correcte,
une révision ancienne et une valeur absente ; les deux derniers cas refusent le
marqueur de réussite et le moteur est toujours libéré.

Après la preuve existante 0010→0013, la recette peuple la vraie base migrée en
0013 par réflexion SQLAlchemy, sans `create_all`. Elle ajoute 30 caches couvrant
le produit des états `complete`, `empty`, `partial`, `failed`, inconnu (`NULL`),
des prix `NULL`, zéro et positif, et d'une date `checked_at` présente ou absente.
Une tentative plus récente que cette date et des montants historiques/annuels
distincts permettent de vérifier qu'ils ne remplacent pas une preuve de collecte.

La migration existante 0014 est ensuite appliquée sans modification. La vérification
dans une autre connexion exige :

- révision 0014 et dialecte PostgreSQL ;
- conservation de toutes les anciennes colonnes et lignes, y compris les tables
  vides, ainsi que des définitions PostgreSQL des contraintes et index ;
- backfill de `last_successful_collection_at` à partir de `checked_at` uniquement
  pour les états admissibles, sans utiliser la date de tentative ;
- absence de date historique et de métadonnées d'alerte inventées ;
- colonnes JSONB et dates avec fuseau nullables, provenance `is_historical`
  booléenne non nullable, initialisation et défaut à `false` ;
- insertion explicite de `true` conservée et rejet de `NULL` avec SQLSTATE 23502 ;
- aller-retour JSONB/date d'alerte dans une transaction annulée.

Les insertions de sonde sont annulées par savepoints. La migration, `app/` et les
tests métier antérieurs ne sont pas modifiés par ce complément.

## Ressources et exécution PostgreSQL

Daemon : `unix:///Users/aminezegmou/.docker/run/docker.sock`. Image locale épinglée :
`sha256:c05eced0bdb41ea9b95a656472a6aa4d50cad0d8a2e33d14eb1c53fd6204f2ae`.
Port de recette : `127.0.0.1:55514`. Aucun téléchargement, montage personnel,
configuration personnelle PostgreSQL ou base existante utilisé.

Commande depuis le worktree du correctif :

```sh
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py postgres \
  --target tests/integration/test_market_refresh_state.py \
  --target tests/integration/test_bd_preparation.py \
  --target tests/integration/test_annual_market_reference.py \
  --target tests/integration/test_series_pricing_concurrency.py \
  --target tests/integration/test_isbn_removal.py \
  --target tests/integration/test_alert_market_panel.py
```

Les identifiants système SQL concordent avec ceux lus dans les conteneurs neufs :

| Exécution | Conteneur | Identifiant système | Résultat |
| --- | --- | --- | --- |
| Initiale, avant corrections de typage de la sonde | `02f36a0e7531afc944fc63e7ec97fbaafdcbe6df7246a36e8c884f4b55fde9ac` | `7697407998185627689` | preuve et 106 tests réussis, pytest 75,98 s, durée murale 108,77 s |
| Finale | `1290d033f68dab32e3cf731c0a9743f25f9b2f7e2e648ab0a867c8c24ed1407d` | `7697408865563439146` | preuve et 106 tests réussis, pytest 86,60 s, durée murale 110,28 s |

La preuve de migration est une étape du lanceur avant pytest ; ses 30 cas ne sont
pas ajoutés artificiellement au nombre de tests pytest. Le rapport final collecte
106 scénarios, sans échec ni scénario ignoré ; le test des deux transactions de
cotation concurrentes a `setup`, `call`, `teardown` réussis et vérifie le dialecte.
PostgreSQL est en version 16.15. Les 137 opérations de transport autorisées visent
uniquement l'endpoint de recette ; les deux refus réseau et le refus dotenv sont
les sondes de contrôle attendues. Aucune configuration personnelle PostgreSQL,
aucun enfant ni scheduler n'est utilisé.

Les deux marqueurs `FINAL_RESOURCE_REMOVED` sont présents. Les contrôles Docker
filtrés par chaque ID ne trouvent plus de conteneur ; le port est disponible.
Aucun nettoyage global exécuté. Preuves temporaires :
`/tmp/radar-migration14-postgres.txt`, `/tmp/radar-migration14-postgres-final.txt`,
`/tmp/radar-migration14-cleanup-final.json`.

## Autres contrôles après le dernier changement de code

Commandes depuis le même worktree ; `PYTHON` désigne
`/Users/aminezegmou/Projets/bot-radar/.venv/bin/python`.

| Contrôle | Résultat | Durée murale |
| --- | --- | --- |
| `RUFF_CACHE_DIR=/tmp/radar-migration14-ruff /Users/aminezegmou/Projets/bot-radar/.venv/bin/ruff check .` | Réussi | 0,10 s |
| `$PYTHON -B scripts/verify_consolidation.py typecheck` | Réussi, 162 puis 4 fichiers | 81,03 s |
| mypy ciblé sur `scripts/verify_postgresql_recipe.py`, avec `environment("typecheck")` et `guard()` avant import mypy | Réussi, 1 fichier | 32,49 s |
| `make test-isolated PYTHON=$PYTHON` | Interrompu par le lanceur à 240 s, code interne 124, code make 2 ; aucun bilan pytest final | 240,44 s |
| `$PYTHON -B scripts/verify_consolidation.py unit --target tests/integration/test_scheduler_occurrence.py --target tests/unit/test_verify_consolidation.py` | 58 passed, aucun ignoré ; pytest 25,89 s | 34,97 s |

Les 58 scénarios incluent les trois tests scheduler et les trois nouveaux tests du
marqueur. Le rapport de garde indique zéro connexion autorisée et trois schedulers
de test contrôlés. La suite complète n'est pas déclarée verte : le bilan antérieur
de 2 129 réussites ne valide pas cet arbre modifié. Le dépassement est global ;
aucun rapport final ne permet de l'attribuer à un scénario précis ni de conclure
à une régression applicative ou à une cause environnementale établie. Aucun délai,
garde ou test n'a été contourné. Un rejeu complet réussi reste nécessaire avant
livraison.

Actualisation du 17 octobre 2026 : le [diagnostic ciblé du timeout](isolated-suite-timeout-diagnostic.md)
conserve cette tentative interrompue et documente deux rejeux complets réussis,
dont le final à 2 133 passed, 12 skipped, aucun échec. Aucun changement de migration
ou nouveau PostgreSQL n'a été effectué pour ces rejeux. La cause historique reste
indéterminée ; le plafond de 240 s est conservé.

Contrôles intermédiaires : Ruff a signalé deux problèmes de style corrigés dans
l'outillage ajouté ; le typecheck explicite de la recette a signalé trois erreurs
de typage corrigées (variables de sonde), sans modification applicative. Les
tests ciblés initiaux du lanceur donnaient 55 passed en 7,11 s. Les sorties finales
sont conservées sous `/tmp/radar-migration14-{ruff-final,typecheck-final,recipe-types-final,tests-final,targeted-final}.txt`.

## Limites

Ce sont des preuves locales synthétiques exécutées par l'auteur, pas une revue
indépendante ni une CI. Les fournisseurs et transports métier restent simulés.
Cette matrice ne prouve pas toutes les données de production possibles, les
permissions d'un rôle SQL de production, la durée d'un DDL sur une grosse table,
ni la migration sous écritures de production concurrentes. Aucun déploiement,
changement de base Git, commit, push, PR ou merge effectué.
