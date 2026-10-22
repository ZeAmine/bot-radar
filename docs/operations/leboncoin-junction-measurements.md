# Jonction conteneur → tunnel — mesures en lecture seule

Statut : grille de mesures. Toutes les commandes sont **en lecture seule** et
exigent un mandat d'accès. Aucune valeur n'est préremplie : une valeur non mesurée
s'écrit **« à mesurer »**.

Convention : tout élément entre chevrons (`<réseau>`, `<service>`, `<compte>`) est
un **paramètre à remplacer par l'opérateur**, jamais une valeur fournie par ce
dépôt. Les commandes sont données sans leurs paramètres réels par construction.

Chaque ligne indique :

- la **commande** à exécuter ;
- ce que le résultat **prouve** — et ce qu'il ne prouve pas ;
- ce qu'il **ne faut pas recopier** dans un rapport.

> Règle générale : un rapport ne contient **jamais** d'adresse IP publique, de nom
> d'hôte, de nom de clé, d'empreinte, de contenu de `authorized_keys`, ni de la
> moindre sortie brute de `sshd -T`. Il contient le **résultat de la lecture**
> (autorisé / refusé / valeur) et la date de la mesure.

## 1. État du pare-feu

À exécuter sur l'hôte VPS, sous mandat. Les commandes de lecture de `iptables`
peuvent nécessiter des droits ; demander le mandat adapté plutôt que d'élargir
l'accès.

| # | Mesure | Commande | Ce que cela prouve |
| --- | --- | --- | --- |
| 1.1 | Règles en place | `ss -H -ltn` puis lecture des règles actives selon le pare-feu installé | Quels ports écoutent réellement |
| 1.2 | Chaîne `INPUT` | lecture de la chaîne `INPUT` avec numéros de ligne | L'ordre exact des règles, donc si une règle est insérée **avant** le `REJECT` |
| 1.3 | Chaîne `DOCKER-USER` | lecture de la chaîne `DOCKER-USER` | Les règles réellement appliquées au trafic des conteneurs |
| 1.4 | Chaîne `FORWARD` | lecture de la chaîne `FORWARD` | Si le trafic conteneur → hôte est filtré, et où |
| 1.5 | Persistance | présence et nature du mécanisme de sauvegarde du pare-feu | Si une règle ajoutée à chaud survit à un redémarrage |
| 1.6 | Comportement par défaut | politique par défaut des chaînes | Le sort d'un flux non explicitement autorisé |

**Ce que ces mesures ne prouvent pas** : qu'un port soit joignable *depuis le
conteneur*. L'écoute peut exister sur l'hôte et rester refusée au conteneur.

**À ne pas recopier** : les adresses publiques, les adresses de bridge, les
identifiants de conteneurs, la sortie brute complète.

**À mesurer** : l'état constaté de chaque chaîne, la politique par défaut, le
mécanisme de persistance et la date de la mesure.

## 2. Réseaux Docker

| # | Mesure | Commande | Ce que cela prouve |
| --- | --- | --- | --- |
| 2.1 | Réseaux présents | liste des réseaux Docker | Quels réseaux existent et lesquels sont attachés au service |
| 2.2 | Sous-réseau du réseau applicatif | inspection du réseau applicatif, champ de configuration IPAM | La plage d'adresses du réseau, donc le **gateway** réel |
| 2.3 | Gateway du réseau | `docker network inspect <réseau> -f '{{(index .IPAM.Config 0).Gateway}}'` | L'adresse que le conteneur peut viser pour joindre l'hôte |
| 2.4 | Appartenance du service | inspection du service applicatif | Que le conteneur est bien sur ce réseau, et lui seul |
| 2.5 | Options de publication | inspection des ports publiés du service | Si un port de l'hôte est déjà publié vers le conteneur |

**Ce que ces mesures ne prouvent pas** : que le conteneur peut *atteindre* la
gateway. Le pare-feu de l'hôte peut refuser le flux malgré une gateway valide.

**À ne pas recopier** : la valeur de la gateway, la plage du sous-réseau, les noms
de conteneurs internes, les identifiants.

**À mesurer** : le nom du réseau applicatif, la présence du service applicatif,
l'existence de ports publiés — et **la gateway elle-même, à noter hors rapport**.

## 3. Valeurs effectives de `sshd` concernant les redirections

| # | Mesure | Commande | Ce que cela prouve |
| --- | --- | --- | --- |
| 3.1 | Configuration effective | `sshd -T` (lecture) | La valeur **effective**, pas celle du fichier de configuration |
| 3.2 | Redirections distantes | `sshd -T` puis recherche des options de redirection distante | Si un client peut demander une redirection inverse |
| 3.3 | Publication de la redirection | `sshd -T` puis recherche de l'option de contrôle d'hôte de bind | Si la redirection inverse peut écouter sur autre chose que le loopback |
| 3.4 | Restriction de bind | `sshd -T` puis recherche des options `PermitOpen` / `AllowTcpForwarding` applicables au compte du Pi | Ce que le compte du Pi a le droit de joindre |
| 3.5 | Compte du Pi | lecture des options du compte concerné dans la configuration `sshd` | Si des `Match` particuliers s'appliquent à ce compte |

**Attention** : `sshd -T` représente la **configuration interprétée**, pas la
garantie d'application. Une configuration modifiée sans rechargement n'est pas
active. Constater aussi l'heure de démarrage du service.

**Ce que ces mesures ne prouvent pas** : qu'une redirection demandée par le Pi
réussisse. Le refus peut venir de la clé, du pare-feu ou du réseau.

**À ne pas recopier** : la sortie brute de `sshd -T` (elle contient des chemins de
clés et des noms), les noms de compte, les chemins de fichiers de configuration.

**À mesurer** : les valeurs effectives des options de redirection et de bind,
l'heure de démarrage du service, la date de la mesure.

## 4. Restrictions de la clé du Pi

| # | Mesure | Commande | Ce que cela prouve |
| --- | --- | --- | --- |
| 4.1 | Existence d'une entrée dédiée | lecture du fichier `authorized_keys` du compte concerné, sans en recopier le contenu | Si la clé du Pi est identifiable séparément |
| 4.2 | Options de cette entrée | lecture des **noms d'options** présents sur la ligne | Si des options limitent les redirections et les commandes |
| 4.3 | Présence d'une empreinte | comparaison d'empreinte par outil dédié, sans afficher la clé | Que la clé observée est celle attendue |

**Ce que ces mesures ne prouvent pas** : l'effet réel des options, qui dépendent
aussi des `Match` et de la configuration globale. Croiser avec la section 3.

**À ne pas recopier** : la clé, son empreinte complète, le commentaire associé,
le chemin exact du fichier, le nom du compte si le rapport circule au-delà du
strict nécessaire.

**À mesurer** : présence ou absence d'une entrée dédiée (**oui / non**, sans
contenu), noms d'options limitantes (**liste de noms**), correspondance de
l'empreinte (**oui / non**), date.

## 5. État du tunnel

| # | Mesure | Commande | Ce que cela prouve |
| --- | --- | --- | --- |
| 5.1 | Processus de tunnel | liste des processus du compte concerné, filtrée sur le client SSH | Si un tunnel est ouvert, depuis quand |
| 5.2 | Écoute locale | `ss -H -ltn` filtré sur le port du relais | Si l'hôte écoute sur ce port, et sur quelle adresse |
| 5.3 | Écoute sur loopback uniquement ou non | même commande, colonne d'adresse locale | Si l'écoute est limitée au loopback |
| 5.4 | Joignabilité depuis l'hôte | une requête de contrôle **respectant le protocole du relais** | Que la chaîne relais fonctionne vue de l'hôte |
| 5.5 | Joignabilité depuis le conteneur | même contrôle exécuté **dans** le conteneur, sur l'adresse visée | Le point exact où la jonction échoue, s'il échoue |
| 5.6 | Sortie résidentielle | contrôle de sortie par le relais | Que la sortie est bien celle du domicile, et non du datacenter |

**Ce que ces mesures ne prouvent pas** : que la sortie soit acceptée par le site
visé, ni qu'elle le restera. Un tunnel résidentiel n'implique pas l'absence de
challenge.

**À ne pas recopier** : l'adresse publique observée, le nom d'hôte de la cible, le
port exact du tunnel, la ligne de commande complète du processus de tunnel (elle
contient l'hôte et l'utilisateur de destination).

**À mesurer** : tunnel présent (**oui / non**), heure d'ouverture, adresse locale
d'écoute (**loopback / autre**, sans valeur), résultat de 5.4, de 5.5 et de 5.6
(**atteint / refusé / délai dépassé**), date et heure.

## 6. Synthèse à produire

Le rapport final de mesure contient un tableau, et rien de plus :

| Mesure | Résultat | Date et heure | Motif si non concluant |
| --- | --- | --- | --- |
| Pare-feu `INPUT` — ordre | à mesurer | à mesurer | à mesurer |
| Pare-feu `DOCKER-USER` | à mesurer | à mesurer | à mesurer |
| Pare-feu `FORWARD` | à mesurer | à mesurer | à mesurer |
| Persistance pare-feu | à mesurer | à mesurer | à mesurer |
| Réseau applicatif identifié | à mesurer | à mesurer | à mesurer |
| `sshd` — redirection distante | à mesurer | à mesurer | à mesurer |
| `sshd` — contrôle du bind | à mesurer | à mesurer | à mesurer |
| Clé du Pi — entrée dédiée | à mesurer | à mesurer | à mesurer |
| Tunnel ouvert | à mesurer | à mesurer | à mesurer |
| Écoute hôte | à mesurer | à mesurer | à mesurer |
| Joignabilité depuis l'hôte | à mesurer | à mesurer | à mesurer |
| Joignabilité depuis le conteneur | à mesurer | à mesurer | à mesurer |
| Sortie résidentielle | à mesurer | à mesurer | à mesurer |

Une ligne **sans date** est une ligne non mesurée. Elle ne peut pas fonder un choix
de variante.
