# Premier contact supervisé — une seule requête Leboncoin

Statut : **procédure écrite, jamais exécutée**. Aucune requête réelle n'a été
émise vers la place de marché pour produire ce document.

## Objet

Établir, **une seule fois et sous mandat**, si le chemin résidentiel aboutit à une
réponse exploitable : la page de recherche répond-elle, le parseur la lit-il,
combien d'annonces en sortent ?

Ce contact **n'active rien** : ni le collecteur, ni `LEBONCOIN_POLLER_ENABLED`, ni
`LEBONCOIN_PROXY_URL`. Il n'écrit ni en base, ni dans Redis, n'envoie rien par
Telegram, et ne modifie aucune configuration de production.

## Ce que le script publie — et ne publie pas

| Publié | Jamais publié |
| --- | --- |
| Code HTTP | Contenu de la page |
| Présence du marqueur de challenge | Titres, prix |
| Parsing réussi ou non | URL d'annonces |
| Nombre d'annonces extraites | Données de vendeurs |
| Durée de la requête | Corps de réponse, en-têtes |

Le script ne peut publier que cela : il ne conserve ni n'affiche le HTML, et
n'imprime que des compteurs et des catégories.

## Prérequis

| # | Prérequis | Constat |
| --- | --- | --- |
| P1 | Jonction conforme | `python3 scripts/check_junction_topology.py --tunnel-user <compte>` rend **0** |
| P2 | Relais durci actif | Une seule instance, liste blanche `leboncoin.fr` |
| P3 | Supervision en place | `flock` actif, `@reboot` et `*/5` présents |
| P4 | Mandat nominatif | Une exécution, une cible, une fenêtre — écrit noir sur blanc |
| P5 | Fenêtre d'observation | Créneau où le compte rendu peut être écrit **le jour même** |
| P6 | API présente dans l'image | Vérification ci-dessous |
| P7 | Aucune variable `*proxy*` parasite dans le conteneur | Comptage seul, ci-dessous |

Un prérequis manquant **annule** le contact : il ne se rattrape pas après coup.

### Vérifier que l'image expose bien les fonctions utilisées

Le script réutilise `fetch_search_page` et `parse_search_page` du collecteur ;
aucune logique HTTP ni de parsing n'est dupliquée. Avant d'exécuter, constater
que l'image **en service** les expose :

```sh
docker exec radar_app python3 -c "
import inspect
from app.services.leboncoin_scraper import fetch_search_page
from app.services.leboncoin_parsing import parse_search_page
print(inspect.signature(fetch_search_page))
print(inspect.signature(parse_search_page))"
```

Attendu : `fetch_search_page(url, proxy_url=None, page=1)` et
`parse_search_page(html)`. Un `ImportError` ou une signature différente interdit
le contact — le code du dépôt ne serait pas celui de l'image.

### Compter les variables d'environnement « proxy » du conteneur

`libcurl` **lit automatiquement** les variables `http_proxy`, `https_proxy`,
`all_proxy` et leurs variantes en majuscules, même quand un proxy est passé
explicitement. Une telle variable dans le conteneur détournerait la requête et le
contact ne prouverait plus rien.

On les **compte**, on n'affiche **aucune valeur** :

```sh
docker exec radar_app sh -c 'env | cut -d= -f1 | grep -ci proxy'
```

Attendu : `0`. Toute autre valeur se traite **avant** le contact — on identifie le
nom de la variable, jamais son contenu.

Le script lui-même n'importe que la bibliothèque standard au chargement
(`argparse`, `os`, `sys`, `time`, `dataclasses`, `urllib.parse`) ; les modules
applicatifs sont importés au moment de l'exécution réelle. Il peut donc être
transmis par l'entrée standard, sans nouveau déploiement.

## Commande exacte

La valeur du proxy est **fournie au lancement** et **n'est jamais écrite dans le
dépôt**. Elle n'est pas lue depuis le `.env`.

```sh
docker exec -i radar_app python3 - < scripts/leboncoin_first_contact.py
```

En simulation (comportement par défaut) :

```sh
docker exec -i -e LEBONCOIN_FIRST_CONTACT_PROXY_URL="socks5h://<gateway>:1080" \
  radar_app python3 - < scripts/leboncoin_first_contact.py
```

Exécution réelle — **deux** gardes sont exigées : l'option `--execute-once` **et**
la variable de mandat, non vide :

```sh
docker exec -i \
  -e LEBONCOIN_FIRST_CONTACT_PROXY_URL="socks5h://<gateway>:1080" \
  -e LEBONCOIN_FIRST_CONTACT_MANDATE="<référence du mandat nominatif>" \
  radar_app python3 - --execute-once < scripts/leboncoin_first_contact.py
```

Le proxy **ne se passe pas en argument** (`--proxy` est refusé) : une ligne de
commande est visible dans la liste des processus. Il ne vient que de
l'environnement.

Sans `LEBONCOIN_FIRST_CONTACT_MANDATE`, le script rend un code distinct (**3**),
n'émet aucune requête, et le dit sur la sortie d'erreur.

Le schéma doit être **`socks5h://`** : `socks5://` ferait résoudre le nom par le
client, donc depuis le conteneur, et la requête ne sortirait plus par le relais
de la même façon. Le script refuse tout autre schéma.

## Grille de lecture

| # | Constat | Sens | Décision |
| --- | --- | --- | --- |
| R1 | `categorie=reponse`, `parsing=reussi`, `annonces>0` | Le chemin aboutit | **On s'arrête là.** Le contact est concluant ; rien de plus n'est tiré de la réponse |
| R2 | `categorie=zero_annonce`, `parsing=reussi`, `annonces=0` | Page servie, liste vide | **On s'arrête et on rend compte.** Un zéro peut être réel ou une coquille de requête : on ne relance pas pour vérifier |
| R3 | `code_http=403` **ou** `429`, `categorie=blocage` | Refus explicite | **On s'arrête.** Règle ferme ci-dessous |
| R4 | `code_http=200`, `challenge=true`, `categorie=challenge` | Captcha servi sous un statut 200 | **On s'arrête.** Règle ferme ci-dessous |
| R5 | `categorie=echec_reseau` | Le relais n'a pas abouti | **On s'arrête.** On constate la jonction (P1) avant toute conclusion sur la place de marché |
| R6 | `parsing=echoue` | Réponse servie, non lue | **On s'arrête.** Le parseur ne connaît pas cette page ; cela se corrige, mais pas ici |
| R7 | `categorie=dependance_absente` | `curl_cffi` manque dans l'image | **On s'arrête.** Rien n'a quitté la machine : le problème est local, pas côté place de marché. Aucune conclusion n'est tirée sur la sortie résidentielle |

**Pourquoi R4 et R3 sont distincts.** `fetch_search_page` lève
`LeboncoinBlockError("HTTP <statut>")` dès que le statut vaut 403 ou 429 **ou**
que le corps porte le marqueur de challenge. Un captcha servi avec un statut 200
arrive donc sous la forme `LeboncoinBlockError("HTTP 200")`. Le script le range en
`challenge`, jamais en `blocage` : ce sont deux constats différents, et les
confondre fausserait la lecture du terrain.

**Pourquoi R7 est distinct.** Une dépendance absente produit le message fixe
`curl_cffi indisponible`. Le script le compare à l'identique, sans interpréter de
message libre : un échec réseau n'est jamais déduit d'un texte. Une
`LeboncoinBlockError` ou une `LeboncoinCollectionError` d'une autre forme reste
classée en `echec_reseau` ou `blocage`.

### Règle ferme pour 403, 429 et challenge

**En cas de 403, 429 ou de challenge depuis la sortie résidentielle, on s'arrête
et on rend compte.** Aucune modification d'empreinte, de cadence, de proxy ou de
périmètre n'est décidée seule. Aucune seconde tentative, aucun changement de
schéma, aucun contournement n'est tenté pour « voir si ça passe ».

Le script ne peut pas le faire : il n'a ni boucle, ni nouvelle tentative, ni
escalade. C'est une contrainte de conception, pas une bonne intention.

## Ce que cette requête ne prouve pas

- **La tenue dans le temps** : un succès unique ne dit rien de la deuxième heure.
- **Le taux de blocage** : une requête ne fait pas un pourcentage.
- **La latence** utile : une mesure n'est pas une distribution.
- **La fiabilité du matching** : le script compte des annonces, il n'évalue
  aucune correspondance, aucun prix, aucune pertinence.
- **La conformité** : un 200 n'est pas un accord de la place de marché.

## Compte rendu : noter, ne pas noter

**À noter :**

- la date, l'heure, la durée ;
- les six compteurs publiés par le script ;
- le prérequis P1 et son code de sortie ;
- l'état de la supervision au moment du contact.

**À ne pas noter :**

- le contenu de la page, un titre, un prix, une URL d'annonce ;
- des données de vendeur ;
- l'adresse réelle du proxy, du VPS ou de la gateway dans un document versionné ;
- tout extrait de réponse, même pour « illustrer » ;
- une capture d'écran du HTML ou de la liste d'annonces.

Le compte rendu vit hors du dépôt s'il contient l'une de ces valeurs. Le dépôt
n'accueille que des compteurs et des catégories.

## Interdiction faite à l'agent

**Cette exécution réelle est interdite à l'agent sans mandat nominatif de
l'utilisateur.** Aucun assistant ne l'émet de sa propre initiative, pas même avec
un proxy fictif, un hôte inexistant ou « pour vérifier la forme de la commande » :
la forme se vérifie par des tests unitaires à doubles, jamais par une invocation.

## Arrêts

On s'arrête et on rend compte, sans rien décider seul, si :

- le contact rend autre chose que R1 ;
- un prérequis P1–P6 manque ;
- la moindre donnée de page a été affichée par inadvertance.
