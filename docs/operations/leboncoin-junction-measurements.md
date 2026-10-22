# Jonction conteneur → tunnel — mesures en lecture seule

Statut : grille de mesures. Toutes les commandes sont **en lecture seule** et
exigent un mandat d'accès. Aucune valeur n'est préremplie : une valeur non mesurée
s'écrit **« à mesurer »**.

Convention : tout élément entre chevrons (`<réseau>`, `<service>`, `<compte>`) est
un **paramètre à remplacer par l'opérateur**, jamais une valeur fournie par ce
dépôt. Les commandes sont données sans leurs paramètres réels par construction.

## Comment rapporter sans fuite

Les commandes ci-dessous produisent parfois des adresses, des noms de comptes ou
des chemins. **Chaque bloc indique son filtre.** Règle générale :

1. Exécuter la commande **telle quelle**, sur la machine, sous mandat.
2. Ne recopier dans le rapport que la **conclusion** (autorisé / refusé / ordre /
   oui / non), jamais la sortie brute.
3. Quand une valeur doit être connue pour agir (une gateway, une adresse d'écoute),
   la noter **hors du rapport**, dans l'espace de travail de l'opérateur.
4. Pour un extrait qui doit remonter, appliquer le filtre de masquage indiqué :
   seuls les **noms** de règles, d'options ou de chaînes sont conservés.

Un rapport ne contient **jamais** : adresse IP, nom d'hôte, nom de compte, chemin
de clé, empreinte, contenu de `authorized_keys`, ni ligne de commande de tunnel.

Chaque section précise ce que la mesure **prouve** et ce qu'elle **ne prouve pas**.

## 1. Pare-feu

### 1.1 Identifier le moteur actif

```sh
command -v nft; command -v iptables; command -v ufw
```

Un système peut avoir `iptables` comme interface de compatibilité au-dessus de
`nft`. **Lire les deux** quand les deux commandes existent : une règle visible
dans l'un peut être invisible dans l'autre selon l'ordre de parcours.

### 1.2 `nft` — règles complètes

```sh
nft list ruleset
```

**Filtre de rapport** — ne remonter que la structure, sans adresses :

```sh
nft list ruleset | sed -E 's/([0-9]{1,3}\.){3}[0-9]{1,3}/ADRESSE/g' \
  | sed -E 's/[0-9a-fA-F]{0,4}(:[0-9a-fA-F]{0,4}){2,7}/ADRESSE/g'
```

Ce que cela prouve : la présence et l'ordre des chaînes, l'existence d'un rejet
par défaut, les règles d'acceptation du trafic des conteneurs.

Ce que cela ne prouve pas : qu'une règle soit effectivement atteinte — une règle
placée **après** un rejet n'est jamais évaluée.

### 1.3 `iptables` — chaînes utiles

```sh
iptables -L INPUT -n -v --line-numbers
iptables -L DOCKER-USER -n -v --line-numbers
iptables -L FORWARD -n -v --line-numbers
iptables -S
```

**Filtre de rapport** — conserver noms de chaînes, cibles et numéros de ligne,
retirer les adresses :

```sh
iptables -S | sed -E 's/([0-9]{1,3}\.){3}[0-9]{1,3}(\/[0-9]{1,2})?/ADRESSE/g'
```

Ce que cela prouve : l'**ordre exact** des règles avec leurs numéros, donc si une
règle peut être insérée **avant** le rejet ; la politique par défaut.

Ce que cela ne prouve pas : l'état après redémarrage (voir 1.5).

### 1.4 `ufw`, si présent

```sh
ufw status verbose
```

Ce que cela prouve : la politique par défaut et les profils autorisés.

**Attention** : `ufw` peut gérer les mêmes chaînes que `iptables`. Ajouter une
règle en direct sans passer par `ufw` peut la rendre invisible à `ufw status` et
être écrasée au prochain rechargement.

**Filtre de rapport** : ne remonter que `Default:`, `Status:` et les **numéros de
port** avec leur politique, jamais les adresses source.

### 1.5 Persistance

```sh
command -v netfilter-persistent; ls /etc/iptables 2>/dev/null
systemctl is-enabled netfilter-persistent 2>/dev/null
systemctl is-enabled ufw 2>/dev/null
systemctl is-enabled nftables 2>/dev/null
```

Ce que cela prouve : **quel** mécanisme réapplique les règles au démarrage, et
s'il est activé.

Ce que cela ne prouve pas : que le fichier sauvegardé soit à jour. Comparer son
horodatage avec celui du dernier changement de règles.

### 1.6 Matrice de synthèse pare-feu

| Chaîne | Politique par défaut | Rejet présent | Numéro de ligne du rejet | Une insertion avant est possible |
| --- | --- | --- | --- | --- |
| `INPUT` | à mesurer | à mesurer | à mesurer | à mesurer |
| `DOCKER-USER` | à mesurer | à mesurer | à mesurer | à mesurer |
| `FORWARD` | à mesurer | à mesurer | à mesurer | à mesurer |

## 2. Docker

### 2.1 Réseaux présents

```sh
docker network ls
```

Ce que cela prouve : les réseaux existants et leur moteur.

Filtre de rapport : noms de réseaux applicatifs **uniquement** s'ils ne révèlent
pas d'infrastructure sensible ; sinon, rapporter « réseau applicatif identifié :
oui ».

### 2.2 Gateway du réseau applicatif

```sh
docker network inspect <réseau> \
  -f '{{(index .IPAM.Config 0).Subnet}} / {{(index .IPAM.Config 0).Gateway}}'
```

Ce que cela prouve : le sous-réseau et la gateway réels.

**Ne pas recopier la valeur** : noter seulement « gateway connue : oui », et
conserver la valeur hors rapport. Le rapport n'a pas besoin de l'adresse.

### 2.3 Appartenance du service

```sh
docker inspect <service> --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
```

Ce que cela prouve : les réseaux auxquels le service est attaché, et lui seul.

### 2.4 Ports publiés

```sh
docker ps --format '{{.Names}}\t{{.Ports}}' | sed -E 's/([0-9]{1,3}\.){3}[0-9]{1,3}/ADRESSE/g'
```

Ce que cela prouve : quels ports sont publiés sur l'hôte, et sur quelle interface.

Filtre de rapport : ne remonter que « ports publiés : aucun / liste de ports ».

### 2.5 Profondeur des règles Docker

```sh
iptables -t nat -L DOCKER -n --line-numbers
```

Ce que cela prouve : que les règles de publication de ports sont gérées par
Docker dans `DOCKER`, distinctes de `DOCKER-USER` (que l'on contrôle, et qui est
évalué avant le filtrage de Docker).

## 3. `sshd` — options de redirection

### 3.1 Extraction filtrée

`sshd -T` affiche **toute** la configuration effective, y compris des chemins.
Extraire uniquement les options utiles :

```sh
sshd -T | grep -E '^(port|allowtcpforwarding|gatewayports|permitopen|permitlisten|disableforwarding|x11forwarding|allowagentforwarding)'
```

Ce que cela prouve : les valeurs **effectives** des options qui décident si une
redirection inverse est possible et où elle peut écouter.

| Option | Rôle exact |
| --- | --- |
| `AllowTcpForwarding` | Autorise globalement la redirection de ports TCP (`yes`, `no`, `local`, `remote`, ou les deux) |
| `GatewayPorts` | Décide si une redirection **inverse** (`-R`) peut écouter sur autre chose que le loopback (`no`, `clientspecified`) |
| `PermitListen` | Restreint **où** une redirection inverse (`-R`) peut écouter : c'est l'option pertinente pour le tunnel du Pi |
| `PermitOpen` | Restreint **vers quoi** une redirection **locale** (`-L`) peut se connecter ; ne concerne pas la redirection inverse |
| `DisableForwarding` | Coupe **toutes** les redirections ; prime sur les options ci-dessus |

Deux précisions documentaires (OpenSSH) :

- `PermitListen` gouverne **l'écoute** d'une redirection inverse ; `GatewayPorts`
  peut la restreindre **en plus**. Les deux se lisent ensemble, aucune ne suffit seule.
- `PermitOpen` vise les redirections **locales** (`-L`) et `PermitOpen` n'a **pas**
  d'effet sur l'écoute d'une redirection inverse : le citer comme contrôle d'un
  tunnel `-R` serait une erreur d'interprétation.

Ce que cela ne prouve pas : l'effet réel pour le compte concerné, si des blocs
`Match` s'appliquent. Vérifier la configuration interprétée **pour ce compte** :

```sh
sshd -T -C user=<compte>,host=<hôte-client>,addr=<adresse-client> \
  | grep -E '^(allowtcpforwarding|gatewayports|permitlisten|permitopen|disableforwarding)'
```

**Attention** : `-C` exige des valeurs réelles. Exécuter la commande sur place et
ne rapporter que les **noms d'options et leurs valeurs effectives**, jamais les
paramètres saisis au passage.

### 3.2 État du service

```sh
systemctl show -p ActiveEnterTimestamp sshd
```

Ce que cela prouve : si la configuration observée est bien **chargée** (une
modification non suivie d'un rechargement n'est pas active).

### 3.3 Options de l'entrée `authorized_keys` du Pi

Seuls les **noms d'options** sont extraits ; la clé, son commentaire et le chemin
du fichier ne sont jamais affichés.

```sh
grep -h -o -E '^(restrict|permitlisten|permitopen|command|no-port-forwarding|no-pty|from)="[^"]*"' <fichier> || true
```

Pour ne garder que les **noms** :

```sh
grep -h -o -E '^(restrict|permitlisten|permitopen|command|no-port-forwarding|no-pty|from)' <fichier> || true
```

| Option | Rôle |
| --- | --- |
| `restrict` | Coupe tout ce qui n'est pas explicitement réactivé (dont la redirection de ports) |
| `permitlisten` | Autorise **explicitement** une écoute de redirection inverse ; sans `restrict` levé sur ce point, la redirection échoue |
| `no-port-forwarding` | Interdit toute redirection de ports |
| `permitopen` | Restreint les redirections **locales** uniquement (sans effet sur l'écoute inverse) |

Ce que cela prouve : la présence de restrictions et leur nature.

Ce que cela ne prouve pas : que l'option soit la seule contrainte — la
configuration `sshd` (3.1) s'applique en plus.

**Ne jamais recopier** : la clé, son empreinte, son commentaire, le chemin exact,
le nom du compte.

## 4. État du tunnel

### 4.1 Processus de tunnel

```sh
ps -o pid,etime,args -C ssh 2>/dev/null | sed -E 's/[0-9]{1,3}(\.[0-9]{1,3}){3}/ADRESSE/g' \
  | sed -E 's/[^ ]*\.(fr|com|net|org)/HOTE/g'
```

Ce que cela prouve : qu'un tunnel est ouvert, et depuis combien de temps (`etime`).

**Ne jamais recopier** la ligne de commande : elle contient l'hôte et l'utilisateur
de destination. Ne remonter que « tunnel présent : oui / non » et l'ancienneté.

### 4.2 Écoute locale

```sh
ss -H -ltn | awk 'NR>1 {print $4}' | sed -E 's/:[0-9]+$//' | sort -u
```

Ce que cela prouve : sur **quelles adresses** l'hôte écoute, donc si l'écoute est
limitée au loopback ou non.

Filtre de rapport : ne remonter que « écoute loopback seulement : oui / non ».

### 4.3 Joignabilité depuis l'hôte

Contrôle respectant le protocole du relais, sans requête applicative :

```sh
timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/<port>; printf "\x05\x01\x00" >&3; head -c 2 <&3 | od -An -tx1'
```

Ce que cela prouve : le relais répond au salut SOCKS5 depuis l'hôte.

Ce que cela ne prouve pas : que la requête aboutisse à une sortie résidentielle.

### 4.4 Joignabilité depuis le conteneur

```sh
docker exec <service> sh -c 'timeout 5 bash -c "exec 3<>/dev/tcp/<adresse-visée>/<port>; printf \"\x05\x01\x00\" >&3; head -c 2 <&3 | od -An -tx1"'
```

Ce que cela prouve : si la jonction fonctionne vue du conteneur, et **où** elle
échoue si elle échoue.

`<adresse-visée>` n'apparaît jamais dans le rapport : ne remonter que
« atteint : oui / non / délai dépassé ».

### 4.5 Sortie résidentielle

Contrôle de sortie par le relais (domaine de contrôle ajouté explicitement, voir
la [procédure de mise à jour du relais](leboncoin-relay-update.md)).

Ce que cela prouve : que la sortie est celle du domicile.

**Ne jamais recopier l'adresse publique observée.** Ne remonter que
« sortie résidentielle : conforme / non conforme », et l'adresse hors rapport
pour comparaison avant/après.

## 5. Synthèse à produire

| Mesure | Résultat | Date et heure | Motif si non concluant |
| --- | --- | --- | --- |
| Moteur pare-feu actif | à mesurer | à mesurer | à mesurer |
| `INPUT` — ordre des règles | à mesurer | à mesurer | à mesurer |
| `INPUT` — rejet présent | à mesurer | à mesurer | à mesurer |
| `DOCKER-USER` | à mesurer | à mesurer | à mesurer |
| `FORWARD` | à mesurer | à mesurer | à mesurer |
| `ufw` présent | à mesurer | à mesurer | à mesurer |
| Persistance — mécanisme | à mesurer | à mesurer | à mesurer |
| Persistance — activée | à mesurer | à mesurer | à mesurer |
| Réseau applicatif identifié | à mesurer | à mesurer | à mesurer |
| Gateway connue | à mesurer | à mesurer | à mesurer |
| Ports publiés | à mesurer | à mesurer | à mesurer |
| `sshd` — `AllowTcpForwarding` | à mesurer | à mesurer | à mesurer |
| `sshd` — `GatewayPorts` | à mesurer | à mesurer | à mesurer |
| `sshd` — `PermitListen` | à mesurer | à mesurer | à mesurer |
| `sshd` — `PermitOpen` (local) | à mesurer | à mesurer | à mesurer |
| `sshd` — configuration `Match` appliquée | à mesurer | à mesurer | à mesurer |
| `sshd` — configuration chargée | à mesurer | à mesurer | à mesurer |
| Clé du Pi — entrée dédiée | à mesurer | à mesurer | à mesurer |
| Clé du Pi — noms d'options | à mesurer | à mesurer | à mesurer |
| Tunnel ouvert | à mesurer | à mesurer | à mesurer |
| Écoute loopback seulement | à mesurer | à mesurer | à mesurer |
| Joignabilité depuis l'hôte | à mesurer | à mesurer | à mesurer |
| Joignabilité depuis le conteneur | à mesurer | à mesurer | à mesurer |
| Sortie résidentielle | à mesurer | à mesurer | à mesurer |

Une ligne **sans date** est une ligne non mesurée. Elle ne peut pas fonder un choix
de variante.
