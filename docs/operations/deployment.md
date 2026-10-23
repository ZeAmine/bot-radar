# Déployer et reprendre une interruption

Statut : procédure actuelle ; à lire obligatoirement avant un déploiement autorisé.
Périmètre : livraison du code, schéma cible et contrôles de reprise. Cette page
n'autorise ni accès production ni modification de passerelle.

Cible documentée : `${VPS_USER}@${VPS_HOST}` (définis dans l'environnement local,
jamais dans ce dépôt), clé SSH désignée par `$VPS_SSH_KEY`,
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

Les deux transports du déploiement — `rsync` de [`deploy.py`](../../scripts/deploy.py)
et la commande distante de [`preflight_deploy.py`](../../scripts/preflight_deploy.py) —
imposent `StrictHostKeyChecking=yes`. Une clé d'hôte non vérifiée laisserait un
intermédiaire se faire passer pour le VPS et recevoir le code synchronisé ; le
premier contact avec un hôte inconnu se fait donc une fois, explicitement, et
`known_hosts` porte ensuite la référence.

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

## Collecte Leboncoin (sortie résidentielle)

Le collecteur Leboncoin est désactivé par défaut et n'utilise aucune sortie datacenter :
DataDome y répond `403` même avec l'empreinte TLS de Chrome. Sur le VPS, renseigner :

```sh
LEBONCOIN_POLLER_ENABLED=true
LEBONCOIN_POLL_INTERVAL_SECONDS=600
LEBONCOIN_PROXY_ENABLED=true
LEBONCOIN_PROXY_URL=socks5h://<gateway-du-reseau-applicatif>:1080
LEBONCOIN_CATCHUP_MAX_PAGES=2
```

Le port `1080` est un tunnel SSH inverse ouvert par le Raspberry Pi du domicile,
derrière un SOCKS5 en espace utilisateur. Ni root, ni port entrant, ni fournisseur
tiers ne sont nécessaires.

Deux détails de cette URL décident du succès, et aucun des deux ne se lit dans le
code applicatif :

- **`socks5h` et non `socks5`.** Avec `socks5://`, libcurl résout le nom
  lui-même et transmet une **adresse** dans la demande CONNECT ; avec
  `socks5h://`, il transmet le **nom** et laisse le relais résoudre
  ([documentation libcurl](https://curl.se/libcurl/c/CURLOPT_PROXY.html)). Le
  relais refuse toute adresse littérale : la sortie résidentielle serait de
  toute façon court-circuitée, puisque la résolution aurait lieu sur le VPS.
  `socks5://` échoue donc systématiquement, même avec un tunnel sain.
- **L'adresse de la gateway du réseau applicatif, pas `127.0.0.1`.** Dans le
  conteneur, `127.0.0.1` est son propre loopback. La gateway se lit par
  `docker network inspect bot-radar_default -f '{{(index .IPAM.Config 0).Gateway}}'`
  et n'est **pas** versionnée : elle est propre à l'installation.

### Pi, sans sudo

```sh
mkdir -p ~/bot-radar-relay/logs
# copier scripts/leboncoin_residential_relay.py dans ~/bot-radar-relay/
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_tunnel -N '' -C botradar-tunnel
# publier ~/.ssh/id_ed25519_tunnel.pub dans ~/.ssh/authorized_keys du VPS
ssh-keyscan -p 22 "$VPS_HOST" >> ~/.ssh/known_hosts
crontab -e
```

Les trois scripts sont versionnés en gabarits dans `deploy/pi-relay/`, avec leur
**crontab de référence, leurs paramètres et leur procédure d'installation**. Cette
section n'en tient pas de copie : voir
[les gabarits du Pi](../../deploy/pi-relay/README.md).

Le tunnel porte `ExitOnForwardFailure=yes`, `ServerAliveInterval=30`,
`ServerAliveCountMax=3` et `StrictHostKeyChecking=yes` : il se ferme plutôt que de
rester ouvert sans redirection, et un hôte inconnu n'est jamais accepté.

La sonde est **séparée** du relais et ne démarre jamais de seconde instance : sur
deux échecs consécutifs d'une poignée de main SOCKS5 (`05 01 00` → `05 00`), elle
arrête le relais pour que `flock` le relance. Un relais vivant mais incapable de
servir est ainsi réparé par le mécanisme qui garantit l'unicité.

### Ce que la supervision ne couvre pas

- **Une résolution DNS bloquée** n'est pas interrompable : huit résolutions
  bloquées saturent les huit emplacements et le relais cesse d'accepter. La sonde
  le détecte et le redémarre, mais le délai de réparation est celui de `flock`.
- **La saturation est immédiate** : au-delà de huit connexions simultanées, le
  relais ferme la connexion sans réponse SOCKS5.
- Aucun de ces mécanismes ne prouve la sortie résidentielle.

### Contrôle d'accessibilité depuis le conteneur

Le contrôle de dépannage utile porte sur **ce que le collecteur joint réellement**,
depuis `radar_app` et vers la gateway — pas sur le loopback de l'hôte.

**(a) Le relais répond-il sur la gateway ?** Un salut SOCKS5 seul, sans `CONNECT` :
il prouve que la socket accepte et que le protocole répond, sans contacter aucune
destination et sans solliciter la résolution.

```sh
GW=$(docker network inspect bot-radar_default \
  -f '{{(index .IPAM.Config 0).Gateway}}')
docker exec radar_app python3 -c "
import socket, sys
s = socket.create_connection((sys.argv[1], 1080), timeout=3)
s.sendall(bytes([5, 1, 0]))
print('salut:', s.recv(2).hex())
s.close()
" "$GW"
```

Attendu : `salut: 0500`. Toute autre valeur — ou une erreur de connexion — signifie
que la jonction est rompue : voir la section « Dérive de la passerelle » de
[la fenêtre Redis](redis-persistence-window.md). Ce test **ne prouve pas** la sortie.

**(b) Un domaine non listé est-il refusé ?** Envoie une demande `CONNECT` vers un
domaine volontairement absent de la liste autorisée. Le relais doit répondre
`0x02` (`NOT_ALLOWED`), sans contacter la destination. Ce test n'émet aucune
requête vers le domaine visé : il vérifie le refus, pas l'accès.

```sh
docker exec radar_app python3 -c "
import socket, sys
s = socket.create_connection((sys.argv[1], 1080), timeout=3)
s.sendall(bytes([5, 1, 0]))
assert s.recv(2) == bytes([5, 0])
hote = b'example.invalid'
s.sendall(bytes([5, 1, 0, 3, len(hote)]) + hote + (443).to_bytes(2, 'big'))
print('refus:', s.recv(10)[:2].hex())
s.close()
" "$GW"
```

Attendu : `refus: 0502`. Un `0500` signalerait une **liste trop large** ; une
absence de réponse, un relais saturé ou bloqué.

**(c) La sortie résidentielle** ne se prouve pas ici : elle exige une requête
réelle vers un domaine autorisé, donc un mandat et une destination ajoutée
explicitement et **temporairement**. La procédure est
[§4.5 « Contrôle volontaire de sortie »](leboncoin-relay-update.md#45-contrôle-volontaire-de-sortie),
avec retrait du domaine de contrôle à la fin.

### Depuis le conteneur `radar_app` : étape supplémentaire obligatoire

Dans le conteneur, `127.0.0.1` désigne son propre loopback : le proxy doit viser
une adresse **joignable depuis le conteneur**, et cette adresse est la gateway du
réseau applicatif.

La chaîne `INPUT` du VPS n'accepte que `22` avant un
`REJECT --reject-with icmp-host-prohibited` : la jonction exige donc une règle
dédiée. La topologie retenue est celle-ci, et c'est elle que
`scripts/check_junction_topology.py` vérifie :

| Élément | Rôle |
| --- | --- |
| `GatewayPorts clientspecified` sur le compte de tunnel | Autorise le tunnel à écouter sur une adresse choisie par le client |
| `-R <gateway>:1080:127.0.0.1:8080` | La redirection vise la gateway du réseau applicatif |
| `permitlisten="<gateway>:1080"` sur la clé | `sshd` n'accepte cette redirection que pour cette adresse |
| Une règle `INPUT` `-s <sous-réseau> -d <gateway> -p tcp --dport 1080 -j ACCEPT` avant le `REJECT` | Les conteneurs du réseau atteignent ce port |
| `LEBONCOIN_PROXY_URL=socks5h://<gateway>:1080` | Le collecteur l'utilise avec résolution côté relais |

Deux invariants à ne pas perdre :

- **L'adresse est celle de la gateway, pas `127.0.0.1`** ni l'IP publique.
- **La gateway n'est pas épinglée.** Une recréation du réseau peut la changer, et
  la règle de pare-feu comme les deux `permitlisten` viseraient alors une adresse
  morte. La dérive est **détectée** par `scripts/check_junction_topology.py`, et
  corrigée selon la section « Dérive de la passerelle » de
  [la fenêtre de persistance Redis](redis-persistence-window.md).

> **Ne jamais exécuter `docker compose down` sur ce projet sans passer par ce
> document.** Un `down` suivi d'un `up` recrée le réseau, donc peut changer la
> gateway — et laisse la jonction cassée si la correction ne suit pas
> immédiatement.

Sans cette jonction, la collecte échoue en `LeboncoinCollectionError` et alerte
après plusieurs cycles. Ces éléments touchent le pare-feu ou `sshd` : les mettre
en place exige un mandat d'exploitation explicite.

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
Le collecteur Leboncoin peut s'arrêter seul sur blocage durable : voir la
[procédure d'arrêt et de reprise](leboncoin-block-stop.md).

## Portée des preuves

`/health` atteste la vivacité HTTP. Sa sonde IA ne prouve pas une génération ; SQL,
Redis, scheduler et poller ne sont pas contrôlés par cette route. Le script exige
ses autres contrôles ; aucun ne prouve une collecte Vinted ou réception Telegram réelle.
Les recettes historiques `delivery-0010` ne sont pas la procédure courante, et une
preuve locale ne démontre jamais la révision ou la configuration d'une production réelle.
