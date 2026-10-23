# Fenêtre de maintenance — Redis persistant et sous-réseau épinglé

Statut : **procédure écrite, jamais exécutée**. Aucune de ces étapes n'a été
jouée sur le terrain. Chaque commande y est un ordre à donner sous mandat, pas
un constat.

## Pourquoi cette fenêtre existe

Deux changements, chacun avec une conséquence propre :

| Changement | Conséquence | Pourquoi maintenant |
| --- | --- | --- |
| `command:` AOF sur `radar_redis` | Le conteneur est **recréé** | Sans AOF permanent, l'arrêt sur blocage durable ne survit pas à un redémarrage : l'activation à chaud est perdue |
| `ipam` épinglé sur le réseau applicatif | Le réseau peut être **recréé**, donc sa gateway peut changer | Une gateway qui dérive invalide la règle de pare-feu et le `permitlisten` du tunnel, sans qu'aucun test applicatif ne le voie |

Les deux touchent des objets dont l'invariance est contrôlée au déploiement.
C'est pourquoi cette fenêtre est **séparée** d'un déploiement applicatif.

## Ce que la fenêtre ne fait pas

- Elle ne déploie pas de code applicatif.
- Elle ne touche ni PostgreSQL, ni `radar_app`.
- Elle ne modifie ni `sshd`, ni `authorized_keys`, ni les clés du Pi.
- Elle ne modifie pas `LEBONCOIN_POLLER_ENABLED` ni `LEBONCOIN_PROXY_URL`.

## Prérequis

| # | Prérequis | Vérification |
| --- | --- | --- |
| P1 | `RADAR_NETWORK_SUBNET` renseignée dans le `.env` de production | Le sous-réseau du réseau en service, relevé avant la fenêtre |
| P2 | Collecteur arrêté ou inactif | Aucun cycle en cours pendant la recréation |
| P3 | Sauvegarde du volume Redis prise et vérifiée non vide | Taille > 0 et horodatage récent |
| P4 | Sauvegarde de `compose.yml` en service | Empreinte relevée |
| P5 | Fenêtre annoncée | Aucun autre opérateur ne déploie pendant ce temps |

> Le sous-réseau n'est pas versionné : c'est une donnée d'installation. Elle se
> relève sur le réseau en service et se reporte dans le `.env` de production.
> **La reporter avant la fenêtre** : si le réseau est recréé avec une valeur
> vide, Compose refuse de démarrer, ce qui est préférable à une dérive
> silencieuse mais transforme une erreur de saisie en indisponibilité.

## Ordre imposé

### 1. Constat d'avant

```sh
docker network inspect bot-radar_default \
  -f '{{(index .IPAM.Config 0).Subnet}} {{(index .IPAM.Config 0).Gateway}}'
docker inspect radar_redis -f '{{.Id}} {{.Config.Cmd}}'
docker exec radar_redis redis-cli INFO persistence | grep -E '^(aof_enabled|aof_last_bgrewrite_status)'
docker exec radar_redis redis-cli DBSIZE
sudo iptables -L INPUT -n -v --line-numbers
sudo sshd -T -C user=<compte-tunnel>,host=localhost,addr=127.0.0.1 \
  | grep -E '^permitlisten'
```

Relever : sous-réseau, gateway, identifiant du conteneur Redis, `aof_enabled`,
nombre de clés, numéro de ligne de la règle `ACCEPT` du port 1080, valeur de
`permitlisten`. **Ces valeurs sont comparées à la fin ; les recopier dans le
compte rendu de la fenêtre, pas dans le dépôt.**

### 2. Sauvegarde du volume Redis

```sh
docker exec radar_redis redis-cli BGSAVE
docker exec radar_redis redis-cli INFO persistence | grep rdb_bgsave_in_progress
docker run --rm -v bot-radar_redis_data:/data -v "$PWD":/backup \
  alpine tar czf /backup/redis-data-<horodatage>.tar.gz -C /data .
ls -l redis-data-<horodatage>.tar.gz
```

Le `BGSAVE` est **explicite** : sans lui, un volume AOF incomplet serait copié
pendant une écriture. Attendre `rdb_bgsave_in_progress:0` avant la copie.

### 3. Arrêt de la collecte

Arrêter le collecteur **avant** la recréation : un cycle qui écrit pendant la
recréation Redis échouerait et consommerait un cycle de comptage.

Ordre : désactiver le poller, attendre la fin du cycle en cours, constater
l'absence de nouvelle collecte.

### 4. Application du changement

```sh
docker compose config --quiet          # refuse un .env incomplet
docker compose build app
docker compose up -d --no-deps redis   # Redis seul, recréé
docker compose up -d --no-deps app     # app seule, sans réconcilier les deps
```

`--no-deps` est indispensable : sans lui, Compose tenterait de réconcilier
`postgres`, ce que cette fenêtre ne doit pas faire.

### 5. Constat d'après — Redis

```sh
docker exec radar_redis redis-cli INFO persistence \
  | grep -E '^(aof_enabled|aof_last_bgrewrite_status|aof_last_write_status)'
docker exec radar_redis redis-cli CONFIG GET appendonly appendfsync
docker exec radar_redis redis-cli DBSIZE
```

Attendu : `aof_enabled:1`, `appendonly yes`, `appendfsync everysec`, **nombre de
clés ≥ celui d'avant**. Un nombre inférieur est un **échec** : arrêter et
restaurer le volume depuis la sauvegarde de l'étape 2.

### 6. Constat d'après — jonction

```sh
docker network inspect bot-radar_default \
  -f '{{(index .IPAM.Config 0).Subnet}} {{(index .IPAM.Config 0).Gateway}}'
python3 scripts/check_junction_topology.py
```

Le contrôle rend trois issues distinctes : **conforme**, **en écart** (avec la
liste), ou **non vérifié**. « Non vérifié » n'est pas « conforme ».

Si la gateway a changé, **ne pas improviser** : les étapes 7 et 8 doivent viser
la nouvelle valeur, et la commande de tunnel du Pi doit être mise à jour dans la
même fenêtre, sinon la jonction reste cassée.

### 7. Règle de pare-feu

Retirer l'ancienne règle si la gateway a changé, ajouter la nouvelle **avant** le
`REJECT` de `INPUT`, puis persister **le fichier** :

```sh
sudo iptables -D INPUT -s <sous-reseau> -d <ancienne-gateway> -p tcp --dport 1080 -j ACCEPT
sudo iptables -I INPUT <position> -s <sous-reseau> -d <nouvelle-gateway> -p tcp --dport 1080 -j ACCEPT
sudo cp /etc/iptables/rules.v4 /etc/iptables/rules.v4.candidate
# insérer la ligne dans le candidate, puis :
sudo iptables-restore --test < /etc/iptables/rules.v4.candidate
sudo cp /etc/iptables/rules.v4.candidate /etc/iptables/rules.v4
```

Jamais `netfilter-persistent save` à l'aveugle : il réécrit le fichier depuis
l'état courant, y compris les chaînes gérées par Docker.

### 8. Tunnel et clé

Si la gateway a changé : mettre à jour le `permitlisten` de l'entrée
`authorized_keys` du compte de tunnel **et** la `-R` de la commande de tunnel du
Pi, puis recharger `sshd` après `sshd -t`. `reload`, jamais `restart`.

### 9. Reprise de la collecte

Redémarrer le collecteur, vérifier un cycle complet, puis constater les annonces
reçues. La collecte reprend **après** que la jonction soit conforme.

## Vérification finale

```sh
python3 scripts/check_junction_topology.py   # doit rendre « conforme »
```

Trois valeurs doivent concorder : gateway du réseau, `permitlisten` de la clé,
règle de pare-feu. Le script ne prouve **pas** que le relais répond — seulement
que la topologie vise la bonne adresse.

## Rollback complet

| # | Action | Commande |
| --- | --- | --- |
| R1 | Arrêter la collecte | Désactiver le poller |
| R2 | Restaurer le volume Redis | `docker run --rm -v bot-radar_redis_data:/data -v "$PWD":/backup alpine tar xzf /backup/redis-data-<horodatage>.tar.gz -C /data` |
| R3 | Restaurer `compose.yml` | Copier la sauvegarde de P4, vérifier l'empreinte |
| R4 | Recréer Redis sans AOF | `docker compose up -d --no-deps redis` |
| R5 | Restaurer la règle de pare-feu | Remettre le fichier `rules.v4` sauvegardé, `iptables-restore --test`, puis appliquer |
| R6 | Restaurer tunnel et `permitlisten` | Rétablir l'ancienne valeur, `sshd -t`, `reload` |
| R7 | Vérifier le retour | `check_junction_topology.py` conforme **et** nombre de clés Redis égal à l'état d'avant |
| R8 | Reprendre la collecte | Réactiver, constater un cycle |

Le rollback restaure la **configuration d'avant** : AOF absent, sous-réseau non
épinglé. Il ne restaure pas une garantie qui n'existait pas.

## Risques de la fenêtre

| Risque | Portée | Atténuation |
| --- | --- | --- |
| Perte de données Redis | Clés applicatives, pas de source de vérité | P3 + R2 ; nombre de clés comparé avant/après |
| Changement de gateway | Jonction cassée, collecte muette | Étape 6 avant la reprise ; le contrôle échoue explicitement |
| Sous-réseau en conflit avec un autre réseau | Compose refuse de démarrer | `docker network ls` avant ; conflit = arrêt, pas de contournement |
| Recréation de `postgres` par effet de bord | Perte de données PostgreSQL | `--no-deps` obligatoire ; contrôle d'invariance bloquant |
| Indisponibilité pendant la fenêtre | Aucune collecte, alertes Telegram normales | Fenêtre courte et annoncée |

## Ce que cette fenêtre ne garantit pas

- Elle ne prouve pas que la restauration du volume fonctionne : R2 est un
  rollback, pas un test. Un test de restauration réel reste à planifier.
- Elle ne prouve pas la sortie résidentielle ni l'accès à la place de marché.
- Elle ne valide aucun seuil de soak.
