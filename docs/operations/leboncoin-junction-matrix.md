# Jonction conteneur → tunnel — matrice de décision

Statut : matrice d'aide à la décision. Elle ne recommande **aucune** variante.
Le choix appartient à l'opérateur, après les
[mesures en lecture seule](leboncoin-junction-measurements.md) et sous mandat
d'exploitation distinct.

## Le problème

Le collecteur tourne dans le conteneur `radar_app`. Le relais SOCKS5 du Pi est
joignable, depuis l'hôte, sur le loopback. Dans le conteneur, le loopback désigne
le conteneur lui-même : l'URL de proxy en boucle locale ne joint donc rien.
Il faut une jonction explicite entre les deux espaces réseau.

Aucune variante n'est gratuite : chacune modifie soit le **pare-feu**, soit la
**configuration du tunnel**, soit les **deux**.

## Contrainte fixe : le relais reste sur le Pi

Le relais doit rester sur le Raspberry Pi. C'est **le seul endroit d'où la sortie
emprunte la connexion résidentielle**. Exécuter le relais ailleurs (VPS, conteneur)
ne change pas l'origine du trafic : le relais ne fait que retransmettre, c'est le
**point de sortie** qui détermine l'adresse source vue par le site.

Déplacer le relais produirait une sortie datacenter, donc l'échec de l'objectif
même de la collecte. Cette contrainte élimine toute variante où le relais quitte
le Pi — voir les variantes écartées.

## Variantes

### V1 — Le tunnel écoute sur la gateway du réseau applicatif

Principe : la redirection inverse du Pi écoute sur l'adresse de gateway du réseau
Docker, et une règle de pare-feu autorise les conteneurs à joindre ce port.

| Aspect | Description |
| --- | --- |
| Ce qui change | Configuration de la redirection côté client (`sshd` : `GatewayPorts` et `PermitListen`) ; une règle de pare-feu insérée **avant** le rejet de la chaîne `INPUT` ; persistance de cette règle |
| Prérequis mesurés | La chaîne `INPUT` rejette par défaut ; la règle peut être insérée avant le rejet ; la gateway est connue et stable ; `AllowTcpForwarding` autorise le mode inverse ; `GatewayPorts` permet l'écoute hors loopback ; `PermitListen` n'interdit pas l'adresse visée ; l'entrée `authorized_keys` ne porte ni `restrict` bloquant ni `no-port-forwarding` |
| Risques | La règle de pare-feu élargit l'exposition de l'hôte au-delà des conteneurs si elle est écrite trop largement ; une règle non persistée disparaît au redémarrage ; l'écoute hors loopback rend le relais joignable par tout processus de l'hôte ; toute erreur d'ordre dans la chaîne peut supprimer le rejet attendu ; `GatewayPorts clientspecified` ouvre la possibilité d'écoute sur **toute** adresse pour tout client autorisé |
| Contrôles préalables | Mesures 1.2 à 1.6 et 2.2 à 2.4 ; vérifier que la règle cible la seule interface du réseau applicatif, jamais une interface publique ; vérifier qu'aucun autre conteneur ne partage ce réseau sans en avoir besoin |
| Tests de validation | Joignabilité depuis l'hôte **puis** depuis le conteneur (4.3 puis 4.4) ; contrôle de sortie (4.5) ; contrôle qu'un conteneur d'un autre réseau ne joint pas le port ; contrôle après redémarrage simulé de la persistance |
| Rollback explicite | Retirer la règle de pare-feu ajoutée, restaurer la persistance précédente, remettre la redirection inverse sur le loopback seul, constater que l'écoute hors loopback a disparu, constater que le collecteur est bien arrêté |

### V2 — Un conteneur dédié porte la sortie résidentielle

Principe : un conteneur du même réseau Docker porte le client de tunnel et
expose le relais du Pi aux autres conteneurs ; l'application le joint par son nom
de service.

Le relais lui-même **reste sur le Pi** : le conteneur dédié ne fait que relayer
la connexion vers le tunnel. C'est un adaptateur réseau, pas un relais de sortie.

| Aspect | Description |
| --- | --- |
| Ce qui change | Un service supplémentaire dans la composition ; l'URL de proxy du collecteur passe du loopback vers le nom de service ; la gestion du cycle de vie de ce conteneur |
| Comment le tunnel aboutit | Le conteneur dédié établit **lui-même** la connexion SSH inverse vers le Pi et demande la redirection d'un port local du conteneur vers le relais du Pi. Le relais reste donc physiquement sur le Pi ; le trafic conteneur → conteneur dédié → tunnel → Pi → sortie résidentielle |
| Secrets et clés portés | Le conteneur dédié porte sa **propre** paire de clés (ou une clé dédiée montée en lecture seule) et l'hôte du Pi. Ces éléments ne doivent jamais être dans l'image ni dans le dépôt : montage de secret en lecture seule, ou agent. La clé doit être distincte de celle du Pi et limitée par `permitlisten`/`restrict` côté Pi |
| `sshd` concerné | **Oui** : la redirection inverse est demandée par ce conteneur, donc `AllowTcpForwarding`, `GatewayPorts` et `PermitListen` s'appliquent toujours, et l'entrée `authorized_keys` de cette nouvelle clé doit être restreinte |
| Pare-feu concerné | **Non** sur l'hôte pour la jonction : la redirection inverse écoute sur le loopback du conteneur dédié, pas sur l'hôte. Le trafic inter-conteneurs passe par le bridge Docker. Une exposition de l'hôte serait un **défaut** de configuration, pas une nécessité |
| Supervision | Le service doit avoir une politique de redémarrage, être surveillé (conteneur vivant + tunnel établi) et journaliser ses reconnexions. Sans tunnel, il doit échouer visiblement, pas silencieusement |
| Risques | Nouveau composant à superviser et à mettre à jour (surface d'attaque et dette d'exploitation) ; un conteneur qui ne se relance pas coupe la collecte sans bruit ; la configuration du proxy dépend du nom de service ; la sortie résidentielle dépend désormais de la santé de ce conteneur **et** de l'hôte du tunnel ; une clé de tunnel supplémentaire à gérer et à révoquer |
| Contrôles préalables | Mesures 2.1 à 2.5 ; vérifier que le pare-feu de l'hôte n'est **pas** modifié ; vérifier la politique de redémarrage ; vérifier que le conteneur ne publie **aucun** port sur l'hôte ; vérifier que la clé dédiée est restreinte côté Pi |
| Tests de validation | Joignabilité par nom de service depuis le conteneur applicatif ; contrôle de sortie (4.5) ; contrôle qu'aucun port n'est publié sur l'hôte ; contrôle du comportement quand le conteneur dédié est arrêté ; contrôle qu'un conteneur d'un autre réseau ne joint pas le service |
| Rollback explicite | Arrêter et retirer le service dédié, restaurer l'URL de proxy précédente, révoquer la clé de tunnel dédiée, constater le retour à l'état antérieur sans volume ni réseau orphelin |

### V4 — Autre variante

Toute autre approche non listée doit être décrite avant d'être considérée, avec
les mêmes rubriques (risques, contrôles, tests, rollback). Une variante non
décrite n'a ni risque évalué, ni rollback préparé.

### V5 — Ne pas jonctionner

Principe : laisser le collecteur désactivé.

| Aspect | Description |
| --- | --- |
| Ce qui change | Rien |
| Risques | Aucun risque d'exposition ; la fonctionnalité reste indisponible |
| Contrôles préalables | Aucun |
| Tests de validation | Constater que le collecteur est arrêté et qu'aucun cycle n'est tenté |
| Rollback explicite | Sans objet |

## Variantes écartées

Ces variantes sont documentées parce qu'elles reviennent naturellement, et
écartées pour un motif factuel. Elles ne sont pas des options en attente.

### Écartée — `extra_hosts` avec `host-gateway`

| Aspect | Motif |
| --- | --- |
| Ce que cela ferait | Ajouter au conteneur une entrée `/etc/hosts` désignant l'hôte, pour joindre `127.0.0.1` sous un autre nom |
| Motif d'exclusion | Cela ne règle **que la résolution de nom**. Le paquet atteindrait bien la gateway, mais le tunnel n'écoute pas sur la gateway (il écoute sur le loopback de l'hôte) et le pare-feu refuserait le flux. `host-gateway` ne change **rien** à l'écoute ni au filtrage : la jonction resterait cassée |
| Ce qui resterait à faire | Tout le travail de V1 (écoute hors loopback + règle de pare-feu). L'entrée `/etc/hosts` serait alors un simple confort de nommage |

### Écartée — `network_mode: host`

| Aspect | Motif |
| --- | --- |
| Ce que cela ferait | Faire partager au conteneur la pile réseau de l'hôte : le loopback devient celui de l'hôte, et le tunnel serait joignable directement |
| Motif d'exclusion | **Perd le réseau applicatif** : plus de résolution par nom de service, donc les liens entre conteneurs (base de données, cache) tombent ; il faudrait les republier sur l'hôte. **Expose le port applicatif 8000** sur toutes les interfaces de l'hôte, et non plus au seul réseau Docker : c'est une régression d'exposition, pas une simplification |
| Conséquence | Le gain de la jonction serait payé par une exposition publique de l'application et une refonte des liens réseau. Écartée pour ce motif |

### Écartée — relais dans le conteneur applicatif

| Aspect | Motif |
| --- | --- |
| Ce que cela ferait | Faire tourner le relais à l'intérieur du conteneur `radar_app` |
| Motif d'exclusion | Le relais sortirait par la connexion du VPS, **pas** par la connexion résidentielle : l'objectif de la collecte (sortie domicile) serait perdu. Contredit la contrainte fixe ci-dessus. S'ajoute la rupture de l'invariant « un seul processus applicatif » |
| Conséquence | Écartée sans évaluation complémentaire : la contrainte de localisation du relais suffit |

### Écartée — publier le port du tunnel par Docker

| Aspect | Motif |
| --- | --- |
| Ce que cela ferait | Publier le port du relais avec `-p` sur le conteneur |
| Motif d'exclusion | Le relais n'est pas dans un conteneur : il n'y a rien à publier. Publier le port de l'hôte reviendrait à exposer le relais au-delà du réseau Docker, ce qui élargit l'exposition sans nécessité |

## Grille de comparaison

| Critère | V1 | V2 | V4 | V5 |
| --- | --- | --- | --- | --- |
| Relais reste sur le Pi | oui | oui | à évaluer | sans objet |
| Touche le pare-feu de l'hôte | **oui** | non | à évaluer | non |
| Touche `sshd` | oui | **oui** | à évaluer | non |
| Nouvelle clé de tunnel à gérer | non | **oui** | à évaluer | non |
| Ajoute un composant supervisé | non | **oui** | à évaluer | non |
| Écoute hors loopback sur l'hôte | oui | non | à évaluer | non |
| Conserve le réseau applicatif | oui | oui | à évaluer | oui |
| Survit à un redémarrage de l'hôte | si persisté | oui | à évaluer | sans objet |
| Survit à un redéploiement applicatif | oui | oui | à évaluer | sans objet |
| Respecte « un seul processus » | oui | oui | à évaluer | oui |
| Rollback entièrement local | non | oui | à évaluer | sans objet |

Cette grille est un **support de discussion**, pas un classement. Les critères ne
sont pas pondérés et l'absence de recommandation est volontaire. Le nombre de
colonnes diffère volontairement des variantes écartées : celles-ci ne sont pas
comparées, elles sont exclues.

## Ce que le choix doit documenter

Quelle que soit la variante retenue, la décision doit consigner :

1. les mesures qui la fondent, avec date et heure ;
2. les risques acceptés et par qui ;
3. le rollback exact, testé au moins une fois ;
4. la supervision mise en place ;
5. la variante explicitement écartée et pourquoi.

Une variante choisie sans mesures datées n'est pas une décision : c'est une
hypothèse.
