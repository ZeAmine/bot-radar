# Fenêtre de maintenance — persistance Redis

Statut : **procédure écrite, jamais exécutée**. Aucune étape n'a été jouée sur le
terrain. Chaque commande est un ordre à donner sous mandat, pas un constat.

## Objet et périmètre

Un seul changement : donner à `radar_redis` une **persistance AOF permanente**,
par un `command:` dans `compose.yml`.

| Élément | Valeur |
| --- | --- |
| Service recréé | `radar_redis` **uniquement** |
| Commande | `redis-server --appendonly yes --appendfsync everysec` |
| Volume | `redis_data`, conservé |
| Dépendances touchées | aucune (`--no-deps`) |

**Pourquoi** : sans AOF permanent, l'activation à chaud est perdue au redémarrage
du conteneur. Or l'arrêt sur blocage durable repose sur une clé sans expiration :
un redémarrage **lèverait de fait** ce garde-fou, sans décision humaine.

## Ce que cette fenêtre ne fait pas

- Elle ne touche ni `postgres`, ni `radar_app`, ni le code applicatif.
- Elle **ne change pas** le sous-réseau du réseau applicatif (voir
  [dérive de la passerelle](#dérive-de-la-passerelle)).
- Elle ne modifie ni `sshd`, ni `authorized_keys`, ni les clés du Pi.
- Elle ne modifie pas `LEBONCOIN_POLLER_ENABLED` ni `LEBONCOIN_PROXY_URL`.

## Ce que la mesure Compose a établi

Un projet Compose jetable à trois services, sur un réseau par défaut, avec
Docker **29.8.2** et Compose **v5.5.1**, images déjà présentes :

| Test | Résultat mesuré |
| --- | --- |
| Ajout de `command:` puis `up -d --no-deps <service>` | **Seul ce service est recréé.** Nouvel identifiant et nouveau `StartedAt` pour lui ; les deux autres gardent identifiant **et** `StartedAt` inchangés |
| Ajout d'`ipam` (même sous-réseau) puis `up -d --no-deps` | **Tous les conteneurs arrêtés**, réseau non recréé, commande en **échec** |
| Puis `up -d` complet | Toujours en échec : projet **incohérent**, conteneurs non rattachés au réseau |
| Recréation après retrait d'`ipam` | Récupération, conteneurs redémarrés |
| `ipam` posé dès la **création** du projet | Fonctionne, et un `up -d` suivant ne recrée rien |

Conclusion retenue : **la recréation ciblée par `--no-deps` est sûre**, et
**l'épinglage du sous-réseau ne peut pas être ajouté à un projet existant** sans
détruire le réseau et arrêter tous les services. L'épinglage est donc **hors de
cette fenêtre** : c'est une décision distincte, non prise.

### Ce que cette mesure ne prouve pas

- Rien sur la production : autre hôte, autre version de Docker possible, autre
  historique de projet.
- Elle ne prouve pas le comportement d'un **premier** déploiement, seulement
  celui d'un projet déjà créé.
- Elle ne mesure aucune perte de données : les volumes du test étaient vides.
- Elle ne dit rien des autres versions de Compose, ni de `docker compose down`.

## État du volume de production

Le volume de production contient **déjà** l'AOF activé à chaud (`aof_enabled:1`,
posé par `CONFIG SET`), alors que `compose.yml` ne le déclare pas. Conséquences
pour la **première** recréation :

- `appendonlydir/` existe déjà sur le volume ; la recréation ne le crée pas, elle
  le **relit**.
- L'activation à chaud n'était **pas permanente** : un redémarrage du conteneur
  l'aurait perdue. C'est précisément ce que la fenêtre corrige.
- La sauvegarde d'avant doit donc contenir `appendonlydir/`, sinon elle ne décrit
  pas l'état réel.

## Prérequis

| # | Prérequis | Vérification |
| --- | --- | --- |
| P1 | Fenêtre annoncée, aucun autre opérateur | — |
| P2 | Collecteur arrêté ou inactif | Aucun cycle pendant la recréation |
| P3 | Sauvegarde du volume Redis, non vide **et restauration vérifiée sur un Redis jetable** (§2 bis) | Taille > 0, clés retrouvées |
| P4 | Sauvegarde de `compose.yml` en service | Empreinte relevée |
| P5 | Accès aux droits nécessaires au constat de jonction | Voir [dérive](#dérive-de-la-passerelle) |

## Ordre imposé

### 1. Constat d'avant

```sh
docker inspect radar_redis -f '{{.Id}} {{.Config.Cmd}}'
docker exec radar_redis redis-cli INFO persistence \
  | grep -E '^(aof_enabled|aof_last_bgrewrite_status|aof_last_write_status)'
docker exec radar_redis redis-cli DBSIZE
docker exec radar_redis redis-cli CONFIG GET appendonly appendfsync
```

Relever : identifiant du conteneur, `aof_enabled`, nombre de clés,
`appendonly`/`appendfsync`. Recopier ces valeurs dans le compte rendu de la
fenêtre — **pas** dans le dépôt.

### 2. Sauvegarde du volume

L'image de sauvegarde est **celle de Redis déjà présente** : aucune image à
télécharger, donc aucune dépendance réseau pendant la fenêtre.

```sh
docker exec radar_redis redis-cli CONFIG GET dir
docker exec radar_redis redis-cli BGSAVE
# attendre la fin du BGSAVE avant de copier
docker exec radar_redis redis-cli INFO persistence | grep rdb_bgsave_in_progress
```

Le `BGSAVE` est **explicite** : sans lui, l'archive serait prise au milieu d'une
écriture. Attendre `rdb_bgsave_in_progress:0`, puis archiver le volume avec
l'image Redis elle-même, **sans réseau** :

```sh
docker run --rm --network none \
  -v bot-radar_redis_data:/data \
  -v "$PWD":/backup \
  redis:7-alpine \
  tar czf /backup/redis-data-<horodatage>.tar.gz -C /data .
ls -l redis-data-<horodatage>.tar.gz
```

**Ce que contient réellement le répertoire, mesuré sur `redis:7-alpine`** avec
l'AOF actif : un répertoire `appendonlydir/` (manifeste, fichier `.incr.aof` et
`.base.rdb`) **et** un `dump.rdb`. Il n'y a **pas** de fichier `appendonly.aof` à
la racine — Redis 7 range l'AOF dans `appendonlydir/`. Une archive qui ne
contiendrait qu'un `dump.rdb` sur un Redis à AOF actif serait incomplète.

### 2 bis. Vérifier la restauration avant la fenêtre

Ce test se fait **avant** la fenêtre, sur un Redis jetable, jamais sur la
production. Il prouve que l'archive est exploitable, ce qu'une simple taille > 0
ne prouve pas.

```sh
# volume vierge, aucun réseau
mkdir -p /tmp/restore-check
docker run --rm --network none \
  -v bot-radar_redis_data:/data -v "$PWD":/backup \
  redis:7-alpine tar czf /backup/redis-data-<horodatage>.tar.gz -C /data .
docker run --rm --network none \
  -v /tmp/restore-check:/data -v "$PWD":/backup \
  redis:7-alpine sh -c 'tar xzf /backup/redis-data-<horodatage>.tar.gz -C /data && ls /data'
docker run -d --name restore_check --network none \
  -v /tmp/restore-check:/data redis:7-alpine \
  redis-server --appendonly yes --appendfsync everysec
sleep 4
docker exec restore_check redis-cli DBSIZE
docker exec restore_check redis-cli INFO persistence | grep '^aof_enabled'
docker rm -f restore_check
```

Critère : `DBSIZE` **égal** au nombre de clés relevé dans l'étape 1, et
`aof_enabled:1`. Sinon la sauvegarde n'est pas utilisable : **ne pas ouvrir la
fenêtre**.

**Résultat de ce test, exécuté sur Docker local** : Redis jetable `redis:7-alpine`
sans réseau, 2 clés écrites, `BGSAVE`, archive de 480 octets contenant
`appendonlydir/` et `dump.rdb` ; restauration dans un volume vierge : `DBSIZE` = 2,
valeurs identiques, `aof_enabled:1`.

**Limites de ce résultat** : deux clés seulement, sur une image locale et un
volume local. Il prouve que **la mécanique** archive → volume vierge → clés
retrouvées fonctionne, pas qu'un volume de production volumineux se restaure
dans le délai de la fenêtre. Il ne mesure ni la durée, ni l'espace disque requis
pour une archive réelle.

### 3. Arrêt du collecteur

Désactiver le poller **avant** la recréation : un cycle qui écrit pendant la
recréation échouerait et consommerait un cycle de comptage. Constater l'arrêt
effectif avant de continuer.

### 4. Recréation de `radar_redis` seule

```sh
docker compose config --quiet          # refuse une configuration incomplète
docker compose up -d --no-deps redis
```

`--no-deps` est **indispensable** : sans lui, Compose réconcilierait les
dépendances, et rien dans cette fenêtre ne doit toucher `postgres`.

### 5. Constat d'après — Redis

```sh
docker inspect radar_redis -f '{{.Id}}'
docker exec radar_redis redis-cli INFO persistence \
  | grep -E '^(aof_enabled|aof_last_bgrewrite_status|aof_last_write_status)'
docker exec radar_redis redis-cli CONFIG GET appendonly appendfsync
docker exec radar_redis redis-cli DBSIZE
```

Attendu : identifiant **différent** (le conteneur a été recréé), `aof_enabled:1`,
`appendonly yes`, `appendfsync everysec`, **nombre de clés ≥ celui d'avant**.

### 6. Constat d'après — les autres services

Les identifiants de `radar_postgres` et `radar_app`, relevés à l'étape 1, doivent
être **inchangés**. Un changement signale que `--no-deps` n'a pas été respecté :
arrêter et examiner avant de reprendre.

### 7. Santé de l'application après la recréation

Redis a été recréé : le pool de connexions de `radar_app` a vu sa cible
disparaître puis revenir. Trois constats, dans cet ordre :

```sh
docker inspect radar_app -f '{{.State.Health.Status}}'
docker exec radar_app python3 -c "import redis, os; r=redis.Redis.from_url(os.environ['REDIS_URL']); print('ping', r.ping())"
docker logs --since 5m radar_app 2>&1 | grep -ciE "connectionerror|redis.*(refused|down)"
```

1. **Santé** : `healthy`. `starting` ou `unhealthy` interdit la reprise.
2. **Connexions Redis** : un `PING` répond `True` depuis le conteneur applicatif.
   C'est le constat que le pool a bien repris, et non seulement que le serveur
   écoute.
3. **Un cycle de poller à vide** : redémarrer le collecteur, laisser passer un
   cycle sans annonce à collecter, et vérifier qu'il se termine sans erreur de
   connexion dans les journaux. Un cycle réussi prouve la reprise ; un échec la
   contredit.

Un cycle « à vide » **n'appelle pas la place de marché** : c'est un cycle sans
annonce à traiter, pas un cycle qui interroge le site.

### 8. Reprise de la collecte

Réactiver le collecteur et constater les annonces.

## Critères d'échec

| # | Constat | Décision |
| --- | --- | --- |
| F1 | `aof_enabled:0` après recréation | **Échec** : le `command:` n'a pas pris. Rollback. |
| F2 | Nombre de clés inférieur à celui d'avant | **Échec** : données perdues. Rollback depuis la sauvegarde. |
| F3 | `radar_postgres` ou `radar_app` recréé ou redémarré | **Échec** : `--no-deps` non respecté. Rollback et analyse. |
| F4 | `aof_last_bgrewrite_status` en `err` | **Échec** : la réécriture AOF ne aboutit pas. Rollback. |
| F5 | Jonction « en écart » après la fenêtre | **Arrêt** : voir [dérive](#dérive-de-la-passerelle) avant toute reprise. |
| F6 | Le collecteur ne reprend pas un cycle | **Échec d'exploitation** : examiner avant d'activer quoi que ce soit. |
| F7 | `radar_app` n'est pas `healthy`, ou le `PING` Redis échoue, ou les journaux montrent des erreurs de connexion après la recréation | **Échec** : le pool n'a pas repris. Ne pas activer ; examiner, puis rollback si nécessaire. |

## Rollback complet

| # | Action | Commande |
| --- | --- | --- |
| R1 | Arrêter la collecte | Désactiver le poller |
| R2 | Restaurer le volume | `docker run --rm -v bot-radar_redis_data:/data -v "$PWD":/backup redis:7-alpine tar xzf /backup/redis-data-<horodatage>.tar.gz -C /data` |
| R3 | Restaurer `compose.yml` | Copier la sauvegarde de P4, vérifier l'empreinte |
| R4 | Recréer Redis sans AOF | `docker compose up -d --no-deps redis` |
| R5 | Vérifier le retour | `aof_enabled` revient à son état d'avant, nombre de clés égal à celui de l'étape 1, identifiants de `postgres` et `app` inchangés |
| R6 | Reprendre la collecte | Réactiver, constater un cycle |

Le rollback restaure **la configuration d'avant**, c'est-à-dire sans persistance
permanente. Il ne restaure pas une garantie qui n'existait pas.

## Dérive de la passerelle

La jonction conteneur → tunnel vise l'adresse de la **gateway du réseau
applicatif**. Cette adresse n'est pas épinglée : une recréation du réseau peut la
changer, et trois éléments deviennent alors caducs — la règle `INPUT`, le
`permitlisten` de la configuration `sshd`, celui de la clé du compte de tunnel.

### La détecter

```sh
python3 scripts/check_junction_topology.py --tunnel-user <compte-de-tunnel>
```

Le script est en **lecture seule**. Il compare la gateway du réseau, la règle
`INPUT` et les deux `permitlisten`. **Il exige des droits de lecture sur
`iptables` et `sshd -T`** : sans eux il rend « non vérifié » avec un code dédié,
jamais « conforme ».

Quatre codes de sortie, à ne pas confondre :

| Code | Sens | Conduite |
| --- | --- | --- |
| 0 | Conforme | Rien à faire |
| 1 | Écart constaté | Corriger, voir ci-dessous |
| 2 | Relevé impossible | Le contrôle n'a pas conclu ; ne pas conclure à sa place |
| 3 | Droits insuffisants | Relancer avec les droits requis |

### La corriger

Si la gateway a changé, l'ordre est : **règle de pare-feu**, puis `permitlisten`
de `sshd`, puis `permitlisten` de la clé, puis la commande de tunnel du Pi.

1. **Règle `INPUT`** : autoriser le sous-réseau vers la **nouvelle** gateway,
   port 1080, avant le `REJECT`. Persister le **fichier** (`rules.v4`) après
   `iptables-restore --test`. Jamais `netfilter-persistent save` à l'aveugle : il
   réécrit le fichier depuis l'état courant, chaînes Docker comprises.
2. **`permitlisten` de `sshd`** : mettre à jour la directive du compte de tunnel
   pour qu'elle vise la nouvelle adresse. `sshd -t` puis **`reload`**, jamais
   `restart`.
3. **`permitlisten` de la clé** : mettre à jour l'option dans l'entrée
   `authorized_keys` du compte de tunnel, sans changer la clé elle-même.
4. **Commande de tunnel du Pi** : la `-R` doit viser la nouvelle adresse. Voir
   le gabarit `deploy/pi-relay/run_tunnel.sh`.
5. Rejouer `check_junction_topology.py` : le retour attendu est **0**.

### Règle absolue sur `docker compose down`

**Ne jamais exécuter `docker compose down` sur ce projet sans passer par ce
document.** Mesuré en local : un `up -d` qui tente de recréer le réseau arrête
**tous** les conteneurs, et le projet ne se rétablit pas tant que le réseau n'est
pas recréé. Un `down` suivi d'un `up` recrée le réseau, donc **peut changer la
gateway** — et laisse la jonction cassée si les étapes ci-dessus ne suivent pas
immédiatement. Sur un projet vivant, cela coupe la collecte et, potentiellement,
l'accès aux données applicatives.

## Ce que cette fenêtre ne prouve pas

- Elle ne prouve pas que la **restauration** du volume fonctionne : R2 est un
  filet, jamais exercé.
- Elle ne prouve pas la sortie résidentielle ni l'accès à la place de marché.
- Elle ne valide aucun seuil de soak.
- Elle ne traite pas la dérive de la passerelle : elle la **détecte** et la
  documente, sans l'épingler.
- L'AOF `everysec` protège jusqu'à environ une seconde d'écritures perdues, pas
  zéro.
