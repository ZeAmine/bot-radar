# H1 — vérification isolée des occurrences scheduler

Début : 16 octobre 2026 ; contrôles finaux : 17 octobre 2026.
Lot local ciblé, sans livraison Git ni service externe.
Branche : `fix/scheduler-verification`. Base locale et HEAD :
`2dee719b5b5ffe44a961e5912a5edd08f79b05f9` ; aucun rafraîchissement distant.
Worktree : `/Users/aminezegmou/Projets/bot-radar-worktrees/scheduler-verification`.

## Cause et correction

Le garde du lanceur autorisait quatre tests de cycle de vie, mais aucun des trois
tests d'occurrence. Ces derniers créaient un scheduler produit complet, avec des
jobs de préparation immédiatement dus. Ajouter leurs seuls noms à une liste
d'autorisation aurait laissé ces autres jobs s'exécuter : cette solution est exclue.

Dans le seul dispositif de test :

- Le test conserve le cron, son listener d'occurrence, l'enveloppe réelle et
  l'exécuteur asyncio. La collecte terminale reste doublée. L'instance est réduite
  à `refresh_prices_07_00` ; le callable de test appelle l'enveloppe originale.
- Le garde autorise uniquement les trois nodeids exacts, un `AsyncIOScheduler`
  natif avec ce seul job coroutine issu du module de test, des exécuteurs asyncio
  et des job stores mémoire. L'autorisation est refusée dans les enfants.
- Le retard synthétique de une seconde est placé dans une grâce de cinq secondes
  propre au test ; aucune configuration produit n'est changée. L'arrêt attend
  l'événement de fermeture, et le lanceur vérifie toujours les tâches terminées.
- Le test qui distinguait prochaine exécution et occurrence effective lit désormais
  la prochaine date du job réellement démarré et l'exige non nulle ; il n'utilise
  plus un scheduler non démarré avec assertion conditionnelle.

`app/tasks/scheduler.py` et l'ensemble de `app/` restent inchangés dans ce lot.
L'exception de test ne désactive ni garde ni transport. Elle n'est pas une sandbox
contre du code de test malveillant ; les règles et transports du lanceur subsistent.

## Preuves avant correction

Commande depuis le worktree H1 :

```sh
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py unit --target tests/integration/test_scheduler_occurrence.py
```

Résultat : **3 failed en 0,17 s**, mêmes nodeids que le correctif produit, refus
`Scheduler réel interdit` puis `SchedulerNotRunningError` au nettoyage.
Sortie : `/tmp/radar-h1-red.txt`.

Les premiers contrôles de protection ajoutés donnent **1 failed, 8 passed en
0,19 s** avant modification du garde : les configurations non isolées sont déjà
refusées ; le cas d'autorisation isolée reste refusé. Sortie :
`/tmp/radar-h1-guard-red.txt`.

## Contrôles ciblés et limites

Les trois tests d'occurrence et les tests de cycle de vie passent après correction.
Une première sonde supplémentaire a échoué : le scheduler enfant était appelé sans
boucle asyncio et le refus socket de la sonde modifiait les compteurs stricts de la
suite. La sonde utilise maintenant sa propre exécution isolée et une boucle dans
l'enfant. Les assertions des compteurs principaux ne sont pas modifiées.
Cette exécution intermédiaire donnait **1 failed, 63 passed en 3,79 s**
(`/tmp/radar-h1-green1.txt`).

La sélection occurrence/lanceur/lifespan donne ensuite **64 passed en 4,37 s**.
Les neuf configurations de refus comprennent nodeid inconnu, origine produit,
mauvais job, job supplémentaire, exécuteur ou store non admis, callable synchrone,
sous-classe et scheduler produit complet. Le dernier contrôle a été ajouté après
cette sélection de 64 scénarios et figure dans la validation finale.

La sonde d'autorisation vérifie HTTP et Redis refusés avant connexion, socket
refusé, puis socket et scheduler refusés dans un enfant. Elle exige les compteurs
précis et zéro connexion autorisée. Aucun PostgreSQL, Redis, Telegram, fournisseur,
serveur ou démarrage de l'application n'est utilisé. Le scheduler est uniquement
une instance asyncio en mémoire pilotée par les tests.

Les nouveaux tests de refus installent eux-mêmes configuration factice et garde
avant les imports applicatifs dans leurs processus dédiés. Ils ne dépendent donc
pas de l'installation préalable du garde par le lanceur parent. Cela préserve leur
usage dans la recette pytest de CI ; la CI elle-même n'a pas été exécutée.

## Validation finale après le dernier changement de code

Commandes depuis le worktree H1, dans cet ordre. `PYTHON` ci-dessous désigne
`/Users/aminezegmou/Projets/bot-radar/.venv/bin/python`.

| Commande | Résultat | Durée murale |
| --- | --- | --- |
| `RUFF_CACHE_DIR=/tmp/radar-h1-ruff-cache /Users/aminezegmou/Projets/bot-radar/.venv/bin/ruff check .` | Réussi | 0,01 s |
| `$PYTHON -B scripts/verify_consolidation.py typecheck` | Réussi, 162 puis 4 fichiers | 19,26 s |
| `make test-isolated PYTHON=$PYTHON` | 2 072 passed, 12 skipped, 4 warnings ; aucun échec ; pytest 76,12 s | 80,36 s |
| `$PYTHON -B scripts/verify_consolidation.py unit --target tests/integration/test_scheduler_occurrence.py` | 3 passed en 0,07 s | 2,43 s |

Sorties : `/tmp/radar-h1-final-ruff.txt`, `/tmp/radar-h1-final-typecheck.txt`,
`/tmp/radar-h1-final-tests.txt`, `/tmp/radar-h1-final-scheduler.txt`.
Les lanceurs terminent avec le code zéro. Les attestations indiquent zéro connexion
autorisée, sept démarrages de scheduler contrôlés dans la suite complète (trois
occurrences et quatre cycles de vie), puis trois dans la sélection occurrence.
Les dix nouveaux scénarios de garde passent dans la suite complète.

Les trois refus historiques sont ainsi remplacés par trois succès sur la branche
H1. Le résultat annoncé du correctif produit (3 failed, 2 115 passed) n'est pas
rejoué ici : son diff n'est pas dans cet espace. Aucun résultat PostgreSQL, CI,
fournisseur ou production n'est revendiqué.

## Préservation et intégration proposée

Le correctif `/Users/aminezegmou/Projets/bot-radar-worktrees/reliable-refresh-add`
reste à `5cbba4c9520459ce69decebbd2bf1be2b6cc950a`, non committé et index vide.
Une empreinte initiale du diff suivi et des six fichiers non suivis est conservée
dans `/tmp/radar-h1-protected.json` ; sa comparaison finale vérifie leur invariance.
Ses rapports et preuves historiques ne sont pas modifiés depuis H1.
La comparaison finale est identique à l'empreinte initiale : diff suivi et six
fichiers non suivis préservés, HEAD et branche inchangés, index toujours vide.
Le patch `/tmp/radar-h1-delivery.patch` est préparé sans staging ni commit.
`git apply --check` réussit dans le worktree produit ; aucun patch n'y est appliqué.
Ce contrôle prouve seulement l'applicabilité textuelle, pas la validation combinée.

Intégration à effectuer seulement sous mandat ultérieur :

1. Vérifier l'état final des deux espaces et le diff H1 ; conserver les travaux.
2. Appliquer le patch H1 contrôlé aux fichiers d'outillage/tests/documentation de
   la branche produit, sans toucher à ses autres modifications ni à l'index.
3. Traiter séparément la mise à jour de sa base locale : le `main` local est passé
   à `2dee719` (#124), sans que cela prouve l'état distant ou un déploiement.
   Toute intégration Git doit être explicitement autorisée ; aucun rebase ici.
4. Rejouer Ruff, typecheck, suite isolée et les trois tests ciblés sur l'arbre
   produit combiné. Les résultats du lot H1 seul ne valident pas cet arbre.
5. Obtenir les preuves PostgreSQL et CI sous leurs mandats, puis seulement les
   autorisations Git, de fusion et de déploiement correspondantes.

La correction et son contrôle sont une auto-revue ciblée, pas une revue indépendante.
H2 et les réserves fournisseurs/production ne sont pas rouverts.
