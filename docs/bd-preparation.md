# Préparation durable des nouvelles alertes BD

ADR-043 reste la règle : une nouvelle BD est préparée avant surveillance. Une
préparation terminée signifie que les tentatives catalogue/cote nécessaires sont
revenues et que leurs écritures requises ont réussi. Elle ne signifie ni ISBN
obtenu, ni prix trouvé. Catalogue absent, autorité indisponible et cote vide sont
des résultats admissibles ; aucune identité ou cote n'est inventée.

## Contrat durable

`alerts.preparation` est nullable. Les anciennes lignes restent SQL NULL, sans
reclassification. Les nouvelles demandes contiennent :

- `token` : version unique du travail ;
- `state` : `pending` ou `blocked` ;
- `activation_requested` : intention indépendante du statut `PAUSED` ;
- `author_hint` : auteur demandé utile à la recherche, jamais auteur confirmé ;
- `requested_at`, `last_attempt_at` : dates UTC pour observation et progression ;
- `error_code` : `catalog_write`, `pricing_write`, `activation_write` ou
  `unexpected`, jamais un texte d'exception.

Une réussite active l'alerte et efface le marqueur dans le même commit. Une pause
invalide le jeton et retire l'intention sans perdre le contexte/erreur. Une reprise
explicite remet une demande inachevée en pending ; une pause ordinaire NULL garde
son ancienne reprise directe. Changer l'ISBN ou le titre d'une veille textuelle
invalide le travail et retire ses anciennes liaisons/verdict/total. Prix, conditions,
mots-clés et titre d'affichage d'une alerte conservant son ISBN ne relancent pas la
préparation. Les livres et leurs caches restent utilisables indépendamment.

Les résultats et les actions utilisateur partagent des transactions courtes avec
verrou de l'alerte et condition sur le jeton/intention/statut. Aucun réseau sous ce
verrou. Une suppression ou version périmée donne un abandon normal. Une cote déjà
engagée peut finir dans sa propre session, sans rétablir une identité ancienne.

## Reprise et limites

Un travail partagé par alerte, deux préparations simultanées. Une annulation du
caller ne tue pas le travail partagé ; une interruption du travail reste pending.
Le scheduler unique lance une passe au démarrage, puis toutes les cinq minutes :
quatre demandes maximum, pending et intention présente seulement. Les travaux
en cours sont exclus ; les passes ne se chevauchent pas. Le tri par dernière
tentative (NULL d'abord), puis création/ID, fait progresser les demandes suivantes.

Une erreur technique devient blocked et attend une relance explicite. Si la DB
ne peut pas enregistrer blocked, la version est suspendue localement et seule cette
écriture est retentée, sans nouvelle collecte. Si le processus meurt avant toute
persistance de cet échec, le prochain processus ne peut connaître cette erreur :
la demande durable pending peut être tentée à nouveau. L'erreur reste journalisée.

Le travail repris ne dépend d'aucun FSM ou message Telegram. La carte /show ou
/list est la source courante ; pas de confirmation Telegram durable « exactement
une fois ». Une erreur d'affichage ou de suggestion IA ne conditionne pas l'activation.

## Retour arrière : maintenance obligatoire, pas seulement inhibition du scheduler

Cette procédure est préparée, **non exécutée sur la production dans le lot**.

1. Avant toute restauration, fermer l'entrée des updates Telegram au niveau de la
   passerelle, avec une réponse non-200 empêchant leur exécution. Cela doit couvrir
   tous les messages/callbacks, y compris ceux déjà dans une file locale. Ne pas
   simplement masquer le bouton /resume. Interdire aussi les actions opérateur de
   modification d'alertes pendant cette maintenance.
2. Arrêter `radar_app` et attendre sa terminaison : le scheduler et les préparations
   nouvelles sont ainsi arrêtés. Aucun second processus. Les collectes annulées
   ne doivent plus écrire avant la conservation des demandes.
3. Avec un outil de maintenance compatible et les accès explicitement autorisés,
   ne traiter que les lignes avec marqueur non NULL : appliquer la sémantique de
   `PreparationRequest.suspend()` (jeton renouvelé, intention false, contexte et
   erreur conservés) et conserver PAUSED. Ne toucher à aucune ancienne pause NULL.
   Ni suppression de marqueur ni restauration de données.
4. Restaurer le code/image voulu **sans downgrade SQL**, en conservant la colonne
   et la révision `0010_alert_preparation`. L'ancien ORM peut ignorer la colonne,
   mais l'ancien /resume peut toujours activer une demande gelée : c'est démontré
   par `test_old_orm_can_read_but_its_resume_bypasses_even_a_frozen_request`.
   **Les updates Telegram restent bloqués pendant toute l'exploitation de l'ancien
   code** ; l'inhibition des seules reprises automatiques ne suffit pas.
5. Ne falsifier ni version Alembic ni contrôles : le graphe Alembic de l'image
   d8da825 s'arrête à 0009 et ne connaît pas 0010. La procédure habituelle de
   déploiement/vérification peut refuser ce retour. Si elle refuse, arrêter et
   préparer un build de retour compatible conservant le graphe 0010, avec revue
   et autorisation, au lieu de contourner les contrôles.
6. Rouvrir les commandes seulement après retour d'un code qui comprend les
   intentions et après vérification du code, santé, schéma, scheduler unique et
   demandes conservées. Leur reprise exige alors un geste explicite ; les anciennes
   pauses ambiguës ne sont jamais reclassées automatiquement.

Il s'agit d'un retour arrière sous restrictions, pas d'une promesse de continuité
fonctionnelle avec l'ancien code. La migration refuse un downgrade destructeur.

## Vérification locale de la transition 0009 → 0010

Rejeu sur PostgreSQL 16 jetable, avec le venv verrouillé du dépôt, vrai démarrage
Uvicorn/scheduler, vrai `/health`, poller désactivé et frontières externes doublées :

- schéma 0009, puis démarrage cible dans l'ancien ordre de `make deploy` :
  `ProgrammingError`, colonne `alerts.preparation` absente, dans la reprise due
  au démarrage ; **le healthcheck HTTP répond pourtant 200** ;
- migration 0010 puis reprise dans ce même processus : réussite, activation et
  effacement du marqueur ;
- sur une autre base neuve, migration avec le code cible avant son démarrage :
  aucune erreur de reprise au démarrage, santé 200 et préparation réussie.

Le healthcheck reste exécuté sans contournement ; il ne contrôle actuellement pas
le schéma DB. L'ancien ordre expose donc un intervalle de code incompatible avec
la DB, même si le déploiement finit ensuite par réussir.

**Correction intégrée dans `scripts/deploy.py` :** pré-vol, arrêt confirmé de l'app,
synchronisation, construction de l'image cible sans démarrer l'application,
migration ponctuelle utilisant explicitement cette image, puis démarrage et attente du vrai healthcheck,
vérification stricte, notification. Exemple des étapes intermédiaires :

```sh
set -eu
cid=$(docker compose ps -a -q app)
test -n "$cid"
docker compose stop app
test "$(docker inspect --format '{{.State.Running}}' "$cid")" = false
# Synchroniser avec les exclusions habituelles seulement après cet arrêt confirmé.
docker compose build app
docker compose run --rm --no-deps --pull never -T --entrypoint alembic app upgrade head < /dev/null
docker compose run --rm --no-deps --pull never -T --entrypoint alembic app current < /dev/null
docker compose up -d --no-build --pull never app
# Attendre healthy, contrôler /health, puis make verify-deploy.
```

Le one-shot ne lance ni scheduler ni poller et ne vise pas l'ancien conteneur.
L'échec de construction empêche la migration ; l'échec de migration ou de lecture
de révision arrête la chaîne avant démarrage cible. Aucun contrôle de santé ou de
révision n'est retiré. AGENTS.md décrit ce même ordre. L'échec d'arrêt ou de sa
confirmation empêche toute synchronisation ; l'échec de synchronisation empêche
le build. L'ancienne application est arrêtée avant toute écriture dans ses bind mounts.
L'interruption couvre synchronisation, construction, migration et redémarrage
jusqu'à la santé réelle, sans durée garantie. Un échec après arrêt peut laisser
l'app arrêtée et les fichiers runtime partiellement synchronisés. Un échec de santé ou de vérification
après démarrage laisse la cible potentiellement en cours : la procédure s'arrête
sans arrêt ou restauration automatique. Les tests de
procédure exécutent les scripts shell avec Docker doublé ; ils ne constituent
pas un déploiement éprouvé sur le Compose de production.

Après un échec, le bilan opérateur doit relever l'état réel du conteneur, les
empreintes du code et la révision effective (one-shot d'une image compatible si
l'app est arrêtée), sans supposer qu'une étape échouée n'a rien changé. Ne pas
relancer l'ancienne image sur les bind mounts partiellement synchronisés.
Si le schéma est resté intégralement à 0009, restaurer d'abord le code et l'image
vérifiés d8da825, sans restauration de données, puis contrôler avant réouverture.
Si le schéma est à 0010, appliquer le retour compatible ci-dessous : graphe 0010
conservé, maintenance Telegram, conservation des marqueurs et restrictions de
réouverture. Si le schéma ou l'état du code est incertain, rester arrêté et faire
vérifier la restauration ; aucun downgrade ni falsification de révision.

L'orchestration `rollback.sh` préparée suppose une app encore en service pour
ses premiers contrôles HTTP. Si elle est déjà arrêtée, ne pas la relancer pour
satisfaire cette précondition : le chemin de restauration depuis cet état doit
être adapté et revu pendant la préparation du déploiement. Les composants de
retour restent disponibles ; Caddy réel, image de production et orchestration
complète restent non éprouvés. Aucun retour en production n'est déclenché.

## Artefact de retour exécutable

```sh
python scripts/prepare_f04_rollback.py --base d8da825 --target <commit-F04-validé> \
  --output /chemin/nouveau-repertoire
```

L'artefact contient le code app/scripts parent, le graphe Alembic cible **0010**
(cible historique de ce lot ; la tête courante du dépôt est `0013_pause_actions`,
voir `AGENTS.md`),
un manifeste d'empreintes, `Dockerfile.rollback`, `rollback.sh` et deux outils :

- `freeze_bd_preparations.py` : lecture seule par défaut ; sous maintenance et
  arrêt vérifié, retire l'intention et renouvelle le jeton. Contexte, dates,
  état et erreur sont conservés ; anciennes lignes NULL et suppressions ignorées.
  Refuse tout schéma autre que 0010 ou une demande marquée hors pause.
- `serve_f04_rollback.py` : démarre l'ancien code dans un seul processus avec
  une garde ASGI **toujours active** renvoyant 503 aux entrées Telegram.
  Pas de commutateur permettant de rouvrir `/resume` avec le code ancien.

`rollback.sh PROJECT_DIR LOCAL_RUNTIME_IMAGE CADDY_MAINTENANCE_CONFIG ORIGIN
--execute` exige une image déjà présente et une configuration complète de
maintenance Caddy préparée/revue. Il construit sans téléchargement, contrôle
health 200 et Telegram 503, arrête app, vérifie l'arrêt, gèle les demandes,
restaure seulement les répertoires de code, puis vérifie la santé, le graphe 0010,
l'absence de worker, les empreintes et le hash des marqueurs. Le résidu
`push_protected.py` est protégé et non exécuté. Aucun downgrade, aucun volume DB
ou `.env` ciblé, aucune déclaration trompeuse du commit parent pour ce build mixte.

**Éprouvé localement :** gel de deux demandes pending/blocked, maintien des pauses
NULL, conservation du contexte et des dates, second gel idempotent ; ancien code
avec graphe 0010 démarré sur PostgreSQL 0010, santé HTTP 200, Telegram 503,
aucun dispatch de l'update synthétique et aucune reprise F04 présente.

**Non éprouvé :** activation de la configuration réelle Caddy, construction de
l'image de retour avec le runtime exact de production, exécution complète de
`rollback.sh` sur le Compose de production. Aucun accès production effectué.
La garde ASGI a été testée, pas une configuration Caddy supposée équivalente.

**Réouverture :** jamais avec cet ancien code. Revenir à un code comprenant F04
et validé, contrôler schéma/empreintes/santé/scheduler et demandes conservées, puis
rouvrir la passerelle avec accord explicite. Les demandes gelées attendent une
relance explicite ; aucune reclassification des anciennes pauses.

## Comparaison contrôlée des benchmarks

Parent `d8da825`, code de la branche `9eb8bfc`, même venv, `PYTHONHASHSEED=0`,
mêmes doubles, SQLite en mémoire et caches pytest/bytecode désactivés. Trois
processus séparés par arbre, alternés parent/branche. Serveurs et PostgreSQL du
harnais arrêtés avant ces mesures ; aucun contrôle ni travail agent en parallèle.
Seuls les deux scénarios précédemment hors budget ont été exécutés.

| Exécution | Sélection parent p95 | Sélection branche p95 | Liaison parent p95 | Liaison branche p95 |
| --- | ---: | ---: | ---: | ---: |
| 1 | 1,50 ms | 1,61 ms | 77,31 ms | 15,47 ms |
| 2 | 1,77 ms | 1,21 ms | 33,20 ms | 11,87 ms |
| 3 | 1,21 ms | 1,24 ms | 10,99 ms | 12,28 ms |

Budgets inchangés : sélection 5 ms, liaison 100 ms. Les six exécutions sont
réussies. Aucune régression reproductible dans cet échantillon ; variabilité
observée aussi sur le parent, sans cause attribuée. Aucune mesure de production.
