# Déployer et reprendre une interruption

Statut : procédure actuelle ; à lire obligatoirement avant un déploiement autorisé.
Périmètre : livraison du code, schéma cible et contrôles de reprise. Cette page
n'autorise ni accès production ni modification de passerelle.

Cible documentée : `ubuntu@88.96.56.44`, clé `~/.ssh/ssh-key-2026-09-10.key`,
répertoire `~/bot-radar/`, domaine `https://radar.aminezegmou.fr`.
La cible est un arbre rsync, **pas un dépôt Git** : un push ne déploie rien.
Le gateway Caddy partagé vit hors dépôt dans `~/gateway/`, sur `web_gateway`.
Ne pas réintroduire un Caddy local ni modifier les autres applications du gateway.

Le dispositif pilote CAL est retiré. Le nettoyage ciblé de son ancienne
configuration suit la [procédure de retrait](chasse-normal-operation.md), sous
mandat de livraison distinct. Le chantier CAL reste sans prérequis de sauvegarde,
GPG ni restauration de contrôle.

## Préconditions et commande canonique

Les contrôles complets de code prévus dans [AGENTS](../../AGENTS.md) doivent
être satisfaits. La confirmation de merge ne vaut pas autorisation de déploiement :
cette dernière doit être explicite. Relire [deploy.py](../../scripts/deploy.py),
[preflight_deploy.py](../../scripts/preflight_deploy.py) et la
[procédure BD](../bd-preparation.md), notamment pour une reprise ou un retour.
Exécuter soi-même les opérations autorisées, sans demander à l'utilisateur de
copier-coller des commandes. Sans permission réseau/clé, signaler le blocage.

```sh
make deploy
```

Cette commande : pré-vol → arrêt et confirmation de l'arrêt de l'ancienne app →
rsync → construction de l'image cible sans app → migration ponctuelle de cette
image via entrypoint Alembic → lecture de révision → démarrage → santé et contrôles
stricts. Aucun scheduler ou poller dans le conteneur de migration. Arrêt au premier
échec ; pas de lancement de l'étape suivante. `DEPLOY_ARGS=--dry-run` affiche l'ordre.
`make preflight` et `make verify-deploy` restent disponibles pour les étapes distinctes.
Ne pas recopier une autre séquence de commandes à côté de celle du script.

Compose utilise le code embarqué dans l’image, sans montage `app/` ou `scripts/`.
Les trois paramètres PostgreSQL sont obligatoires. Le pré-vol vérifie le modèle
Compose cible avec la configuration distante avant arrêt ; après démarrage, les
empreintes du code sont aussi vérifiées dans le conteneur actif. Cela ne prouve
ni construction reproductible, ni réception Telegram, ni récupération opérationnelle.

Le rsync exclut `.git` et `.env` : il préserve la configuration distante et ne
transfère pas le secret local. Contrôler `APP_ENV=production`, code synchronisé,
conteneur, schéma et scheduler. `.env.example` ne doit jamais écraser un `.env` réel.

### Les dépendances ne font pas partie du déploiement

Un déploiement applicatif ne touche ni PostgreSQL ni Redis, et le script le
**vérifie** au lieu de le supposer.

Le fait observé, le 16 octobre 2026 : `make deploy` a recréé `radar_postgres`
(`Created` passé à `2026-10-16T01:02:47Z`) alors qu'il ne visait que `app`. Redis,
lui, n'a pas bougé (conteneur `ca2d0d87…`, créé le `2026-09-25`), et le volume
`bot-radar_postgres_data` n'a pas été recréé : aucune donnée n'a été perdue.

Le mécanisme identifié : Compose réconcilie les dépendances d'un service avant de
le démarrer. `up -d app` applique donc aussi `postgres` et `redis`, et recrée tout
service dont l'empreinte de configuration a changé.

La cause exacte du changement d'empreinte, **prouvée** : c'est la commande du
`healthcheck` de `postgres`, modifiée par #117. La preuve n'est pas la chronologie
mais la comparaison des configurations résolues. Les deux versions résolvent le
**même** environnement (`POSTGRES_USER/PASSWORD/DB` identiques) et ne diffèrent que
par `pg_isready -U radar_user -d radar_db` devenu `pg_isready -U "$${POSTGRES_USER}"
-d "$${POSTGRES_DB}"`. Or `docker compose config --hash postgres` rend
`cd74a20e…` pour la définition déployée — exactement l'empreinte portée par le
conteneur en service — contre `413c8c26…` pour la précédente. La chronologie seule
n'aurait rien établi : le `.env` modifié une minute plus tôt ne change pas
l'environnement résolu, et l'attribuer à la syntaxe `${VAR:?}` serait faux.

*(Provenance : ces éléments proviennent de l'investigation en lecture seule
autorisée avant l'implémentation. Aucun accès serveur supplémentaire n'a été
effectué pour écrire cette page.)*

La parade est `--no-deps` sur le démarrage applicatif, et l'invariance est
contrôlée : `Id`, `StartedAt` et l'état d'exécution des deux services sont relevés
avant l'arrêt de l'app, puis relus après son démarrage. Un écart — recréation,
redémarrage ou disparition — fait **échouer** le déploiement et est nommé
explicitement. Aucune réparation automatique n'est tentée. Les dépendances sont
aussi vérifiées opérationnelles **avant** l'arrêt : interrompre l'app alors qu'un
service manque ne ferait que rendre le service indisponible pour rien.

Si l'inspection d'après démarrage est **impossible** — commande en échec, relevé
illisible, identité d'avant inconnue — le déploiement échoue aussi, avec un code
non nul, même quand l'application est saine. Le message dit que l'invariance n'a
pas pu être vérifiée et que ni « modifiées » ni « intactes » n'est établi : ne pas
avoir pu regarder ne prouve pas que rien n'a bougé. Aucune réparation, aucun
redémarrage et aucun retour arrière n'est déclenché sur cette seule base.

Cette règle vaut pour les **trois** entrées du contrôle, y compris l'absence
d'identité d'avant : sans relevé initial, il n'y a rien à comparer, donc
l'invariance n'est pas établie et le déploiement échoue. Ce cas ne se produit pas
dans le parcours nominal — l'étape d'arrêt capture toujours avant — mais il est
refusé plutôt que supposé conforme, et un test le vérifie.

Dans ce parcours borné à `app`, un changement de PostgreSQL ou de Redis est **un
écart à signaler**, jamais un comportement autorisé. Modifier volontairement une
dépendance — image, variable, `healthcheck` — relève d'une intervention séparée,
explicite et revue : ce n'est pas un effet de bord acceptable d'un déploiement
applicatif. Le contrôle d'invariance existe précisément pour rendre cet écart
visible au lieu de le laisser passer.

Une simulation ne prouve pas cette invariance. Le `--dry-run` du 16 octobre
affichait les trois conteneurs `Running` sans rien dire de l'écart de
configuration qui allait être résolu : il compare l'état, pas l'effet.

## Échec et retour

L'interruption couvre synchronisation, construction, migration et démarrage jusqu'à
la santé réelle, sans durée garantie. Un échec après arrêt peut laisser le service
arrêté et l'arbre partiellement synchronisé ; ne pas redémarrer automatiquement
l'ancien code sur cet arbre. Après démarrage, la cible peut être active malgré un
contrôle échoué. Constater conteneur, code et schéma avant de choisir la reprise.
Aucune restauration automatique, aucun downgrade destructeur ou version falsifiée.

### Retour arrière de l'application, sans toucher la base

Pour un lot sans migration — c'est le cas des lots #120 et #121, le schéma restant
`0014_alert_market_freshness` — le retour arrière applicatif n'exige aucun
`downgrade` SQL et ne touche ni PostgreSQL ni Redis.

**La référence de retour est l'image réellement en production juste avant le
déploiement.** Elle est capturée automatiquement, dans cet ordre :

1. **Avant le build**, l'identifiant exact de l'image du conteneur `radar_app`
   est lu (`docker inspect --format '{{.Image}}'`), un tag de sauvegarde unique
   `bot-radar-app:rollback-<horodatage>` est créé sur cet identifiant, et le tag
   est **vérifié** en comparant son `.Id` à l'identifiant capturé. Une
   incohérence arrête le déploiement avant l'arrêt de l'app.
2. **Après le build**, la présence de ce tag est revérifiée : c'est le moment où
   `latest` a été réécrit, donc celui où l'image d'avant doit encore être
   atteignable. Un tag disparu se découvrirait sinon au moment du retour
   arrière, c'est-à-dire trop tard.

La copie est nécessaire parce que `docker compose build app` réécrit
`bot-radar-app:latest` **en place**. Le tag conservé reste utile comme filet
historique, mais un tag ancien n'est **pas** le retour du prochain déploiement :
le seul retour immédiat correct est celui créé lors de ce déploiement-là, et son
nom est affiché par le script. `dc88692` est l'état connu de la production
actuelle, pas une valeur à coder en dur.

**Pour restaurer**, utiliser la référence applicative réellement attendue par la
commande Compose, sans se fier au nom d'un tag supposé correct. `compose.yml` ne
porte **aucune** clé `image:` pour `app` : le service est construit, donc Compose
résout `bot-radar-app:latest` à partir du nom de projet (`bot-radar`) et du
service (`app`). Cette résolution ne se lit pas dans `compose config` — `image` y
vaut `null` — et le conteneur, lui, enregistre `bot-radar-app` sans suffixe. La
vérification qui tranche est donc l'identité de l'image, pas la chaîne du tag :

```sh
# Nom de projet utilisé par Compose pour construire la référence.
docker compose config --format json | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["name"])'   # -> bot-radar

# Tag conservé par le dernier déploiement (affiché par le script).
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep 'rollback-'
```

Aligner l'image de retour, puis démarrer **seulement** `app` :

```sh
docker tag bot-radar-app:rollback-<horodatage> bot-radar-app:latest
docker compose up -d --no-build --pull never --no-deps app
```

Contrôler ensuite l'identité de l'image du conteneur redémarré **et** sa santé —
un retagage ne vaut que si le conteneur tourne effectivement sur l'image visée :

```sh
docker inspect radar_app --format '{{.Image}}'
docker inspect radar_app --format '{{.State.Health.Status}}'
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep 'rollback-'
```

Le `--no-deps` est ici aussi indispensable : sans lui, un retour arrière pourrait
recréer une dépendance, exactement comme le déploiement du 16 octobre. Contrôler
enfin que `radar_postgres` et `radar_redis` ont gardé leur `Id` et leur
`StartedAt` — un retour arrière qui a touché la base n'est pas un retour arrière
applicatif.

Ce retour restaure le **code applicatif**, pas la configuration : les paramètres
PostgreSQL obligatoires et le `compose.yml` à jour restent en place, et c'est voulu.

Les anciennes préparations BD exigent une maintenance bloquant les updates Telegram
pour un retour vers un code qui ignore les intentions. Inhiber le scheduler seul
ne protège pas `/resume`. La procédure BD détaille compatibilité et artefact de retour.
La sauvegarde/restauration exige en plus [son propre contrat](../backup-recovery.md).

## Portée des preuves

`/health` atteste la vivacité HTTP. Sa sonde IA ne prouve pas une génération ; SQL,
Redis, scheduler et poller ne sont pas contrôlés par cette route. Le script exige
ses autres contrôles ; aucun ne prouve une collecte Vinted ou réception Telegram réelle.
Les recettes historiques `delivery-0010` ne sont pas la procédure courante, et une
preuve locale ne démontre jamais la révision ou la configuration d'une production réelle.
