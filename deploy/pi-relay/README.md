# Gabarits des scripts du Pi

Ces trois scripts sont la **version de référence** de ce qui est déployé sur le
Raspberry Pi pour la supervision du relais et du tunnel. Ils sont versionnés ici
comme gabarits : aucun ne contient d'adresse, de compte, de chemin de clé ni de
valeur propre à une installation.

## Fichiers

| Fichier | Rôle |
| --- | --- |
| `run_relay.sh` | Lance le relais SOCKS5 en avant-plan, sous `flock` |
| `run_tunnel.sh` | Ouvre le tunnel SSH inverse en avant-plan, sous `flock` |
| `relay_probe.sh` | Sonde de santé : poignée de main SOCKS5 locale, arrêt du relais muet |

Les trois sont en **POSIX `sh`**, pas en `bash` : `cron` exécute avec
`SHELL=/bin/sh`, et un script qui suppose `bash` échoue sur un système où `/bin/sh`
est autre chose.

## Ce que garantit la conception

- **`exec` partout où un processus est supervisé.** Le shell est remplacé par le
  relais ou par `ssh`, donc le descripteur du verrou `flock` reste ouvert aussi
  longtemps que le processus vit. Une invocation concurrente échoue au verrou au
  lieu de démarrer une seconde instance.
- **La sonde n'arrête que ce qui écoute.** Elle relève les PID à l'écoute sur le
  port, envoie `TERM`, et journalise. Elle **ne démarre jamais** de relais :
  c'est le superviseur qui possède l'unicité.
- **La sonde réessaie avant de conclure.** Deux échecs espacés de quelques
  secondes, pour ne pas arrêter un relais sur un incident transitoire.
- **Rien n'est inventé au démarrage.** Aucun paramètre critique n'a de valeur par
  défaut : une installation incomplète échoue avec un code non nul, au lieu de
  viser une adresse supposée.

## Paramètres

Lus depuis l'environnement. Les valeurs sont fournies au déploiement, jamais
écrites dans le dépôt.

| Variable | Utilisée par | Sens |
| --- | --- | --- |
| `RELAY_DIR` | les trois | Répertoire du relais sur le Pi (**obligatoire**) |
| `RELAY_PORT` | `run_relay.sh`, `relay_probe.sh` | Port d'écoute du relais (défaut `8080`) |
| `TUNNEL_KEY` | `run_tunnel.sh` | Clé privée dédiée au tunnel (**obligatoire**) |
| `TUNNEL_REMOTE` | `run_tunnel.sh` | `compte@hôte` du VPS (**obligatoire**) |
| `TUNNEL_LISTEN_ADDRESS` | `run_tunnel.sh` | Adresse d'écoute côté VPS (**obligatoire**) |
| `TUNNEL_PORT` | `run_tunnel.sh` | Port d'écoute côté VPS (défaut `1080`) |
| `PROBE_HOST` | `relay_probe.sh` | Hôte interrogé par la sonde (défaut `127.0.0.1`) |
| `PROBE_MAX_LOG_BYTES` | `relay_probe.sh` | Plafond du journal de sonde (défaut 64 Kio) |
| `PROBE_KEEP_LINES` | `relay_probe.sh` | Lignes conservées après troncature (défaut 200) |

`TUNNEL_LISTEN_ADDRESS` est l'adresse de la gateway du réseau applicatif du VPS,
pas `127.0.0.1` : voir [la fenêtre Redis et sous-réseau](../../docs/operations/redis-persistence-window.md)
pour la dérive de cette valeur.

## Installation

1. Créer l'arborescence du relais et ses journaux. Les scripts écrivent dans
   `$RELAY_DIR/logs/` et **n'échouent pas** si le répertoire manque : ils
   échouent à la redirection. Créer le répertoire avant la première exécution.
2. Copier les trois scripts **sans les modifier** dans le répertoire du relais.
   Ils se contentent de lire l'environnement.
3. Créer les deux fichiers de verrou dans un sous-répertoire dédié.
4. Rendre les scripts exécutables par leur propriétaire, et **non** modifiables
   par les autres utilisateurs.

```sh
mkdir -p "$RELAY_DIR/logs" "$RELAY_DIR/locks"
chmod 700 "$RELAY_DIR" "$RELAY_DIR/logs" "$RELAY_DIR/locks"
chmod 755 "$RELAY_DIR"/run_relay.sh "$RELAY_DIR"/run_tunnel.sh "$RELAY_DIR"/relay_probe.sh
```

Droits attendus : scripts en `755` (le contenu ne révèle aucun secret, il vient
d'un dépôt privé), journaux et verrous en `700` sur le répertoire. La clé privée
du tunnel reste hors de ce répertoire, avec les droits de `ssh`.

## Crontab

**Source de vérité unique** : cette section. `docs/operations/deployment.md` y
renvoie et n'en tient pas de copie.

Deux mécanismes distincts, souvent confondus :

- les lignes **`nom=valeur`** sont lues par `cron` lui-même et deviennent
  l'environnement de la commande ; **cron ne développe pas de variable dans une
  valeur**, `RELAY_DIR=$HOME/x` resterait littéral ;
- le **reste de la ligne** est exécuté par le shell de `SHELL` (ici `/bin/sh`),
  qui, lui, **développe `$RELAY_DIR`** dans la commande.

C'est pourquoi les variables sont définies en tête, puis utilisées sous forme
`$RELAY_DIR` dans les commandes : c'est le shell qui les développe, pas cron.

```sh
SHELL=/bin/sh
PATH=/usr/local/bin:/usr/bin:/bin
RELAY_DIR=/home/<UTILISATEUR>/bot-radar-relay
TUNNEL_KEY=/home/<UTILISATEUR>/.ssh/<CLE_TUNNEL>
TUNNEL_REMOTE=<COMPTE_TUNNEL>@<HOTE_VPS>
TUNNEL_LISTEN_ADDRESS=<ADRESSE_GATEWAY>
RELAY_PORT=8080
TUNNEL_PORT=1080
PROBE_HOST=127.0.0.1

# Valeurs à ne jamais écrire dans le dépôt, reportées ici après installation :
#   UTILISATEUR           compte du Pi qui exécute le relais
#   CLE_TUNNEL            nom de la clé dédiée au tunnel, sans son préfixe de chemin
#   COMPTE_TUNNEL         compte de tunnel sur le VPS
#   HOTE_VPS              hôte du VPS
#   ADRESSE_GATEWAY       gateway du réseau Docker applicatif
```

Chaque ligne `nom=valeur` est lue par cron : **aucune ne doit rester vide ni
commentée en fin de ligne**, un commentaire y serait pris pour une partie de la
valeur.

```sh
@reboot sleep 20; /usr/bin/flock -n $RELAY_DIR/locks/relay.lock $RELAY_DIR/run_relay.sh
*/5 * * * * /usr/bin/flock -n $RELAY_DIR/locks/relay.lock $RELAY_DIR/run_relay.sh
@reboot sleep 25; /usr/bin/flock -n $RELAY_DIR/locks/tunnel.lock $RELAY_DIR/run_tunnel.sh
*/5 * * * * /usr/bin/flock -n $RELAY_DIR/locks/tunnel.lock $RELAY_DIR/run_tunnel.sh
*/5 * * * * /bin/sh $RELAY_DIR/relay_probe.sh
```

Trois points que ces lignes portent :

- **Les deux `@reboot` sont décalés** (20 s pour le relais, 25 s pour le tunnel) :
  le tunnel ne doit pas partir avant que le relais n'écoute.
- **Aucune redirection de journal dans les lignes `cron`.** Les scripts
  redirigent déjà vers `$RELAY_DIR/logs/` ; une seconde redirection créerait deux
  écritures concurrentes sur le même fichier, rendrait le journal illisible et
  pourrait couper en deux une ligne que la sonde tronque.
- **La sonde a sa propre ligne** et ne prend aucun verrou : elle ne modifie pas le
  relais, elle l'arrête pour que le superviseur le relance.

## Vérification après installation

```sh
# une seule instance de chaque
ss -H -ltn | grep -c ":8080"
ps -eo cmd | grep -c "[i]d_ed25519"

# la sonde est silencieuse quand le relais répond
sh "$RELAY_DIR/relay_probe.sh"; echo "code=$?"

# le doublon échoue proprement (code 1), sans démarrer de seconde instance
flock -n "$RELAY_DIR/locks/relay.lock" "$RELAY_DIR/run_relay.sh"; echo "code=$?"
```

## Ce que ces gabarits ne font pas

- Ils ne prouvent pas la sortie résidentielle.
- Ils ne bornent pas le journal du relais ni celui du tunnel : seul le journal de
  sonde est tronqué. Une rotation externe reste nécessaire si ces journaux
  grossissent.
- Ils ne remplacent pas la rotation de la clé ni la vérification du pare-feu.

## Comparaison avec le déploiement

Ces fichiers reproduisent la logique déployée **telle qu'elle a été constatée**.
Aucune comparaison automatique n'est faite entre le dépôt et le Pi : un écart est
possible et ne serait pas détecté. **Aucune valeur n'est affirmée ici** : chaque
ligne se vérifie au prochain accès autorisé, et le relevé se note dans le compte
rendu de cet accès, pas dans ce fichier.

Liste de contrôle, à refaire après toute modification d'un côté ou de l'autre :

| # | À comparer | Où regarder | Écart = |
| --- | --- | --- | --- |
| C1 | Empreinte de chaque script | `sha256sum` côté Pi, `git hash-object` côté dépôt | Le dépôt ne décrit plus le terrain |
| C2 | Contenu du verrou et chemins | `locks/relay.lock`, `locks/tunnel.lock` | Verrous différents = deux superviseurs |
| C3 | Décalages `@reboot` | `crontab -l`, deux premières lignes | Tunnel parti avant le relais |
| C4 | Chemin et interpréteur de la sonde | `crontab -l`, dernière ligne | Sonde non exécutée |
| C5 | Redirection de journal dans les lignes cron | `crontab -l` | Double écriture sur le même fichier |
| C6 | Variables de tunnel effectives | `ps -eo args` sur le processus `ssh` | `-R` visant une autre adresse que la gateway |
| C7 | Options de durcissement de `ssh` | mêmes arguments | `StrictHostKeyChecking` ou `ExitOnForwardFailure` absents |
| C8 | Port d'écoute du relais | `ss -H -ltn` | Relais et tunnel sur des ports différents |
| C9 | Nombre d'instances | `ss` et `ps` comptés | Doublon |
| C10 | Taille des journaux | `logs/` | Journal non borné qui grossit |

Ce que cette liste **ne** fait pas : elle ne prouve pas que le relais sort par une
adresse résidentielle, ni que la place de marché répond. Ces deux points relèvent
du premier contact supervisé, pas d'une comparaison de fichiers.
