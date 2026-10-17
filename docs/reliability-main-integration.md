# Intégration de main dans la PR #128

17 octobre 2026. Branche `fix/reliable-refresh-add`, merge sans rebase ni force-push.
Parents vérifiés avant intégration :

- correctif publié : `d7abc926a9193ad1da310dd87b086ccae1fcc4c2` ;
- `origin/main` : `034f48eb4a646f098737670f25a663993507b529` (#124–#126).

## Résolutions

Le merge avait un conflit textuel dans
`tests/integration/test_scheduler_occurrence.py`. L'auto-fusion du lanceur
juxtaposait aussi deux mécanismes d'autorisation ; elle n'a pas été conservée.

Un seul prédicat d'occurrence combine maintenant :

- nodeids exacts, `AsyncIOScheduler` natif, exécuteur asyncio, stockage mémoire,
  coroutine et un seul job `refresh_prices_07_00` ;
- autorisation des seuls espions du module de test ou de l'enveloppe réelle
  `job_avec_occurrence` ; pour cette dernière, contrôle des opérations métier
  connues référencées contre leur identité dans leurs modules d'origine ;
- refus dans les enfants et maintien des gardes HTTP/Redis/socket et processus.

Le test remplace toutes les opérations métier avant construction, vérifie tous
les jobs construits, puis retire les autres jobs de sa seule instance. Il garde
l'enveloppe réelle, lit la prochaine date pendant l'exécution, exige qu'elle soit
postérieure à l'occurrence reçue, et attend les événements de capture et d'arrêt.
Les assertions cron, `coalesce`, `max_instances` et le cas retardé sont conservés.
La grâce de cinq secondes reste propre au cas de test, sans modification produit.

Les fixtures de refus/autorisation de #125 utilisent désormais l'ID du job
canonique, afin que leurs assertions portent sur la substitution métier et non
sur un refus préalable pour mauvais ID. Leurs assertions sont conservées, ainsi
que les refus de configuration de H1 et ses sondes de transports/enfants.
Tous les noms de tests des deux parents sont présents. L'allowlist de cycle de
vie inclut les neuf cas de #125 ; aucun scheduler produit n'est modifié.

La recherche BnF titre+auteur et son repli uniquement après réponse réussie vide
de #124 sont intégrés avec l'identité canonique et les contrôles d'ambiguïté du
correctif. Les nouveaux tests BnF sont identiques à main. Les fichiers du
diagnostic de chaîne #126 et les tests de cycle de vie sont également identiques
à main ; aucune exécution réelle du diagnostic d'exploitation n'est effectuée.

## Vérifications locales avant commit de merge

Interpréteur : `/Users/aminezegmou/Projets/bot-radar/.venv/bin/python`.
Commandes depuis le worktree du correctif.

| Contrôle | Résultat | Durée murale |
| --- | --- | --- |
| Sélection scheduler, garde, lifespan, BnF et chaîne | 132 passed, pytest 15,30 s | 18,11 s |
| `ruff check .`, cache dans `/tmp/radar-integration-ruff` | Réussi | 0,05 s |
| `python -B scripts/verify_consolidation.py typecheck` | Réussi, 162 puis 4 fichiers | 16,55 s |
| `python -B scripts/verify_consolidation.py unit --target tests --timings /tmp/radar-integration-full-phases.jsonl` | 2 182 passed, 12 skipped, 4 warnings ; pytest 80,92 s | 84,42 s |

La dernière sélection est identique à `make test-isolated`, avec mesures optionnelles.
Les 2 194 scénarios sont collectés ; aucun nodeid du dernier arbre publié n'est
perdu, 49 scénarios de main sont ajoutés. Zéro connexion autorisée dans le parent
et les enfants, douze schedulers de test contrôlés (occurrences et cycle de vie).
Plafonds du lanceur inchangés. Les trois tests scheduler passent.

## PostgreSQL sur l'arbre intégré

Même recette bornée, six cibles fraîcheur/préparation/historique/concurrence/retrait
ISBN/carte, sans téléchargement ni base existante. Port `127.0.0.1:55514` libre et
image locale épinglée vérifiés avant création. Image :
`sha256:c05eced0bdb41ea9b95a656472a6aa4d50cad0d8a2e33d14eb1c53fd6204f2ae`.

Conteneur neuf : `4d320012b1f6c88f31a914b1fb7ee750c2bc5eba7f5ecfcc197a59d4249641cc`.
Identifiant système Docker et SQL concordant : `7697544500740329513`.
PostgreSQL 16.15, base synthétique `radar_pause_recipe`, stockage tmpfs.

Installation à vide 0014 et preuve de migration peuplée 0013→0014 réussies :
30 cas de backfill, conservation des anciennes données et contraintes/index,
nullabilité, défaut et refus de NULL de la provenance historique, JSONB/date.
Puis **106 passed, aucun ignoré**, pytest 13,27 s, recette 18,72 s, code zéro.
Le test des deux transactions concurrentes vérifie le dialecte PostgreSQL et a
ses phases setup/call/teardown réussies. Les seules 137 opérations de transport
autorisées visent l'endpoint synthétique ; les refus de contrôle attendus restent
actifs. Aucun enfant ni scheduler démarré dans cette recette.

`FINAL_RESOURCE_REMOVED` est présent et le contrôle Docker filtré par cet ID
ne retrouve aucun conteneur. Aucun nettoyage global ni volume personnel touché.
Le premier bind après suppression indiquait une adresse occupée, sans listener
selon `lsof` ; le recontrôle final du bind réussit, sans intervention sur un
processus ou sur la configuration du port.
Preuves : `/tmp/radar-integration-postgres.txt` et
`/tmp/radar-integration-postgres-cleanup.json`.

## Portée

Les preuves locales portent sur l'arbre intégré, pas sur une future modification.
La CI doit être consultée sur le SHA du commit de merge publié. Ce contrôle de
l'auteur reste une auto-vérification, pas une revue humaine ou indépendante.
Le timeout historique non expliqué, les limites H2 et Telegram/Redis, ainsi que
l'absence de validation des fournisseurs et de la production restent documentés.
Aucune fusion de la PR dans main, aucun déploiement ni accès production autorisé.
