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

## Variantes

### V1 — Le tunnel écoute sur la gateway du réseau applicatif

Principe : la redirection inverse du Pi écoute sur l'adresse de gateway du réseau
Docker, et une règle de pare-feu autorise les conteneurs à joindre ce port.

| Aspect | Description |
| --- | --- |
| Ce qui change | Configuration de la redirection côté client (`sshd`) ; une règle de pare-feu insérée **avant** le rejet de la chaîne `INPUT` ; persistance de cette règle |
| Prérequis mesurés | La chaîne `INPUT` rejette par défaut ; la règle peut être insérée avant le rejet ; la gateway est connue et stable ; les redirections distantes sont autorisées ; le contrôle de bind permet l'écoute hors loopback |
| Risques | La règle de pare-feu élargit l'exposition de l'hôte au-delà des conteneurs si elle est écrite trop largement ; une règle non persistée disparaît au redémarrage ; l'écoute hors loopback rend le relais joignable par tout processus de l'hôte ; toute erreur d'ordre dans la chaîne peut supprimer le rejet attendu |
| Contrôles préalables | Mesures 1.2 à 1.6 et 2.2 à 2.4 ; vérifier que la règle cible la seule interface du réseau applicatif, jamais une interface publique ; vérifier qu'aucun autre conteneur ne partage ce réseau sans en avoir besoin |
| Tests de validation | Joignabilité depuis l'hôte (5.4) **puis** depuis le conteneur (5.5) ; contrôle de sortie (5.6) ; contrôle qu'un conteneur d'un autre réseau ne joint pas le port ; contrôle après redémarrage simulé de la persistance |
| Rollback explicite | Retirer la règle de pare-feu ajoutée, restaurer la persistance précédente, remettre la redirection inverse sur le loopback seul, constater que l'écoute hors loopback a disparu, constater que le collecteur est bien arrêté |

### V2 — La sortie résidentielle portée par un conteneur dédié

Principe : un conteneur du même réseau Docker porte la sortie résidentielle et
sert de point d'entrée ; l'application le joint par son nom de service.

| Aspect | Description |
| --- | --- |
| Ce qui change | Un service supplémentaire dans la composition ; l'URL de proxy du collecteur ; la gestion du cycle de vie de ce conteneur |
| Prérequis mesurés | Le réseau applicatif est identifié ; la résolution par nom de service fonctionne entre conteneurs ; l'hôte n'a pas besoin d'une écoute supplémentaire |
| Risques | Nouveau composant à superviser et à mettre à jour (surface d'attaque et dette d'exploitation) ; un conteneur qui ne se relance pas coupe la collecte sans bruit ; la configuration du proxy devient dépendante du nom de service ; la sortie résidentielle dépend désormais de la santé de ce conteneur |
| Contrôles préalables | Mesures 2.1 à 2.5 ; vérifier que le pare-feu n'est pas modifié ; vérifier la politique de redémarrage du nouveau service ; vérifier que le conteneur ne publie aucun port sur l'hôte |
| Tests de validation | Joignabilité par nom de service depuis le conteneur applicatif ; contrôle de sortie (5.6) ; contrôle qu'aucun port n'est publié ; contrôle du comportement quand le service dédié est arrêté |
| Rollback explicite | Arrêter et retirer le service dédié, restaurer l'URL de proxy précédente, constater le retour à l'état antérieur sans laisser de volume ni de réseau orphelin |

### V3 — Relais exécuté dans le conteneur applicatif

Principe : le relais tourne dans le même conteneur que le collecteur, sur le
loopback du conteneur.

| Aspect | Description |
| --- | --- |
| Ce qui change | Le déploiement du relais, son ordonnancement, sa supervision |
| Prérequis mesurés | Les redirections inverses sont joignables sur le loopback du conteneur ; le conteneur peut maintenir un processus de fond |
| Risques | Deux processus dans un conteneur censé en contenir un : la supervision et les journaux se compliquent ; un redéploiement du conteneur coupe le relais et le tunnel ; le tunnel doit viser un conteneur recréé à chaque déploiement, donc une cible instable |
| Contrôles préalables | Vérifier l'invariant « un seul processus applicatif » du dépôt ; vérifier que le déploiement ne recrée pas le conteneur à chaque livraison |
| Tests de validation | Joignabilité depuis le conteneur ; contrôle de sortie (5.6) ; contrôle après un redéploiement du conteneur |
| Rollback explicite | Arrêter le relais dans le conteneur, revenir à l'image précédente, constater que le conteneur ne contient plus qu'un processus |

### V4 — Autre variante

Toute autre approche (proxy applicatif, redirection de port par le démon Docker,
résolution par nom d'hôte de l'hôte, etc.) doit être décrite avant d'être
considérée, avec les mêmes rubriques. Une variante non décrite n'a ni risque
évalué, ni rollback préparé.

### V5 — Ne pas jonctionner

Principe : laisser le collecteur désactivé.

| Aspect | Description |
| --- | --- |
| Ce qui change | Rien |
| Risques | Aucun risque d'exposition ; la fonctionnalité reste indisponible |
| Contrôles préalables | Aucun |
| Tests de validation | Constater que le collecteur est arrêté et qu'aucun cycle n'est tenté |
| Rollback explicite | Sans objet |

## Grille de comparaison

| Critère | V1 | V2 | V3 | V4 | V5 |
| --- | --- | --- | --- | --- | --- |
| Touche le pare-feu | oui | **non** | non | à évaluer | non |
| Touche `sshd` | oui | non | non | à évaluer | non |
| Ajoute un composant supervisé | non | **oui** | non | à évaluer | non |
| Écoute hors loopback | oui | non | non | à évaluer | non |
| Survit à un redémarrage de l'hôte | seulement si persisté | oui | dépend | à évaluer | sans objet |
| Survit à un redéploiement applicatif | oui | oui | **non** | à évaluer | sans objet |
| Respecte « un seul processus » | oui | oui | **non** | à évaluer | oui |
| Rollback entièrement local | non | oui | oui | à évaluer | sans objet |

Cette grille est un **support de discussion**, pas un classement. Les critères ne
sont pas pondérés et l'absence de recommandation est volontaire.

## Ce que le choix doit documenter

Quelle que soit la variante retenue, la décision doit consigner :

1. les mesures qui la fondent, avec date et heure ;
2. les risques acceptés et par qui ;
3. le rollback exact, testé au moins une fois ;
4. la supervision mise en place ;
5. la variante explicitement écartée et pourquoi.

Une variante choisie sans mesures datées n'est pas une décision : c'est une
hypothèse.
