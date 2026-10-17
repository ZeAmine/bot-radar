# Diagnostic local du plafond de suite isolée

17 octobre 2026. Branche `fix/reliable-refresh-add`, HEAD local
`5cbba4c9520459ce69decebbd2bf1be2b6cc950a`, base inchangée, index vide.

## Preuves antérieures et limite du diagnostic rétrospectif

| Exécution | Résultat | Durée murale | CPU utilisateur / système |
| --- | --- | --- | --- |
| Dernière réussite avant le complément 0014 | 2 129 passed, 12 skipped, 4 warnings | 80,54 s | 61,23 / 6,85 s |
| Suite après complément 0014 | arrêt global, code interne 124, code make 2 | 240,44 s | 166,42 / 30,90 s |

Sorties préservées : `/tmp/radar-combined-tests.txt` et
`/tmp/radar-migration14-tests-final.txt`. Cette dernière contient aussi un `F`
avant l'interruption, sans traceback ni bilan final. Il ne faut pas la présenter
comme une suite n'ayant rencontré que le timeout global.

La reconstruction des symboles pytest donne 1 470 résultats de progression et
un `F` à l'index 351 (base zéro). Avec l'ordre de collecte connu, il correspond à
`test_webhook_characterization_in_isolated_subprocess`. Le dernier résultat de
progression correspond à `test_anti_block_cooldown_backoff`. Ce sont des repères
de progression, pas des piles d'exécution : ils ne prouvent ni la phase bloquée,
ni le motif du `F`, ni la cause du ralentissement. Les anciens marqueurs
`FINAL_NODE` étaient capturés par pytest. Les anciens fichiers n'ont pas les durées
individuelles nécessaires pour reconstituer ces causes rétroactivement.

## Instrumentation ciblée

Le seul ajout exécutable est une option `--timings` du lanceur `unit`. Elle écrit
dans un fichier neuf des lignes JSON horodatées par horloge monotone, hors capture
pytest : configuration, gardes, départ pytest, fin de collecte, début de test,
chaque rapport setup/call/teardown avec statut et durée, fin pytest et fin suite.
Le chemin est résolu avant le changement de répertoire. Un journal préexistant
à l'invocation est refusé, sans être remplacé ni complété par cette option.

Les huit tests existants des codes de retour vérifient aussi les mesures pour
succès, skip, erreurs setup/call/teardown, erreur de collecte et sélection vide.
Ils exigent l'ordre temporel et la correspondance exacte avec les phases/statuts
pytest. Un test supplémentaire couvre le chemin relatif depuis un autre dossier.

Le plafond parent reste 240 s, l'alarme de scénario reste 20 s ; le test webhook
conserve son propre délai enfant de 30 s. Aucun de ces délais n'est relevé ou
contourné, aucun test n'est désactivé. Aucun correctif applicatif ou de migration,
aucun service réel ni nouveau PostgreSQL utilisé.

## Premier rejeu instrumenté

```sh
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py unit \
  --target tests --timings /tmp/radar-timeout-full-phases.jsonl
```

Résultat : 2 132 passed, 12 skipped, 4 warnings, aucun échec, pytest 76,24 s,
durée murale 79,97 s, CPU utilisateur/système 61,27/6,91 s. Les trois succès
supplémentaires par rapport à la dernière réussite antérieure sont les tests du
marqueur d'installation ajoutés au complément 0014, pas de nouveaux tests métier.

Mesures : gardes prêts à 0,144 s ; départ pytest à 0,220 s ; collecte achevée à
4,535 s (2 144 scénarios) ; fin pytest à 78,478 s ; fin suite à 78,483 s.
Sommes des durées : setup 6,168 s, call 66,023 s, teardown 0,499 s.
Le traitement après retour pytest, incluant nettoyage temporaire et validation
du rapport, prend environ 0,005 s. Le reste inclut collecte, hooks et orchestration.

La phase la plus longue est le test webhook en sous-processus : 8,128 s, réussi.
Viennent notamment les tests de procédures shell de déploiement avec outils
doublés : une phase à 4,473 s, les autres à moins de 2,756 s. Il ne s'agit pas
d'un déploiement réel. Aucun échec ni attente de 20 s n'est reproduit dans ce rejeu.
Le temps CPU et la durée totale sont proches de la réussite antérieure ; cela ne
prouve pas à lui seul une cause environnementale pour le timeout passé.

Le chemin relatif de l'instrumentation a ensuite été corrigé et couvert par un
test ciblé (1 passed en 0,48 s, durée murale 3,06 s), avant les contrôles finaux.

## Contrôles finaux après correction du chemin relatif

```sh
RUFF_CACHE_DIR=/tmp/radar-timeout-ruff /Users/aminezegmou/Projets/bot-radar/.venv/bin/ruff check .
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py typecheck
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py unit \
  --target tests --timings /tmp/radar-timeout-full-final-phases.jsonl
```

La dernière commande utilise la sélection exacte de `make test-isolated`, avec
l'option de mesures en plus ; aucun scénario n'est retiré.

| Contrôle | Résultat | Durée murale |
| --- | --- | --- |
| Ruff | Réussi | 0,04 s |
| Typecheck | Réussi, 162 puis 4 fichiers | 16,83 s |
| Suite complète finale | 2 133 passed, 12 skipped, 4 warnings, aucun échec ; pytest 74,75 s | 78,17 s |

Les 2 145 scénarios sont collectés et leurs rapports vérifiés. Aucun nodeid de la
dernière réussite antérieure n'est perdu. Le succès supplémentaire par rapport au
premier rejeu instrumenté est le test de chemin relatif ; les huit tests de codes
de retour existants sont renforcés, pas remplacés. Les trois tests scheduler
passent. Les compteurs de garde indiquent zéro connexion autorisée dans le parent
et tous les enfants ; sept schedulers de test contrôlés, aucun service réel.

Répartition finale : gardes prêts à 0,097 s, collecte de 4,355 s après le départ
pytest ; setup cumulé 5,426 s, call 65,371 s, teardown 0,431 s. La fin pytest arrive
à 76,827 s et la fin suite à 76,832 s : environ 0,005 s pour le nettoyage temporaire,
le rapport et ses vérifications après retour pytest. Ces sommes ne couvrent pas
les coûts de collecte, de hooks et d'orchestration entre phases.

Le test webhook en sous-processus est la phase la plus longue à 9,201 s, réussie ;
le plus long test de procédure shell avec doubles prend 4,383 s. Aucun long blocage
de préparation ou de nettoyage n'est observé dans les deux rejeux instrumentés.
La durée et le CPU final (59,83/6,36 s) sont proches de la réussite antérieure.
Ces résultats ne démontrent pas la cause de l'incident historique.

Sorties : `/tmp/radar-timeout-ruff-final.txt`,
`/tmp/radar-timeout-typecheck-final.txt`, `/tmp/radar-timeout-full-final.txt`.
Mesures : `/tmp/radar-timeout-full-phases.jsonl`,
`/tmp/radar-timeout-full-final-phases.jsonl` et
`/tmp/radar-timeout-analysis-final.json`. Les sorties historiques sont conservées.
Un dépassement de longueur Ruff intermédiaire dans le nouveau test a été corrigé.

## Limites

La cause précise du timeout historique et du `F` n'est pas établie. Aucun défaut
applicatif ni correction de durée n'est revendiqué. Cette instrumentation rend les
prochaines interruptions localisables sans modifier leur politique de validation.
Une interruption future reste une validation incomplète.

Ce travail est une auto-vérification locale, pas une revue indépendante. CI,
fournisseurs et production ne sont pas validés. Aucun commit, push, PR, changement
de base, merge ou déploiement effectué.
