# Collecte Leboncoin — dossier de préparation à l'activation

Statut : dossier de préparation. Il ne réalise aucune activation, ne donne aucune
permission et n'exécute aucune commande. Chaque mesure et chaque geste décrit ici
doit être exécuté par un humain, sous un mandat distinct et explicite.

Périmètre : la jonction conteneur → tunnel résidentiel, la mise à jour du relais
SOCKS5 sur le Raspberry Pi du domicile, et la validation d'un cycle réel.
Le collecteur et l'alerte sont décrits par [ADR-054](../decisions/marketplace.md)
et la [procédure d'arrêt sur blocage](leboncoin-block-stop.md).

## Ce que ce dossier ne fait pas

- Il ne choisit pas de variante de jonction : voir la [matrice de décision](leboncoin-junction-matrix.md).
- Il ne déploie rien, ne redémarre rien, ne modifie ni pare-feu ni `sshd`.
- Il ne contient aucune adresse, aucun nom d'hôte, aucun chemin de clé réels.
- Il ne vaut pas autorisation : une case cochée n'est jamais une activation.

## Documents

| Document | Contenu |
| --- | --- |
| [Mesures de jonction](leboncoin-junction-measurements.md) | Commandes exactes en lecture seule (`nft`, `iptables`, `ufw`, Docker, `sshd -T`), filtres de masquage, ce que chacune prouve et ne prouve pas |
| [Matrice de jonction](leboncoin-junction-matrix.md) | Variantes V1 / V2 / V4 / V5, variantes écartées et leurs motifs, risques, contrôles, rollback |
| [Mise à jour du relais](leboncoin-relay-update.md) | Préchecks, déploiement, vérification, contrôle de sortie volontaire, rollback |
| [Activation et désactivation](leboncoin-activation-and-rollback.md) | Variables concernées, ordre imposé, redémarrage, vérifications, retour arrière |
| [Validation après activation](leboncoin-activation-validation.md) | Référence initiale, cycle réel, latences exactes, soak, tunnel tombé, blocage durable |
| [Checklist d'activation](leboncoin-activation-checklist.md) | Deux colonnes : dépôt / prérequis opérationnels datés |
| [Fenêtre Redis et sous-réseau](redis-subnet-maintenance.md) | Procédure et rollback de la persistance Redis et de l'épinglage du sous-réseau applicatif |
| [Mandats requis](#opérations-exigeant-un-mandat-séparé) | Les **13** opérations hors de ce dossier |

Les documents ont les dépendances suivantes :

```
mesures ──→ matrice ──→ (mandat d'exploitation)
                            ├──→ mise à jour du relais ──┐
                            └──→ jonction ───────────────┤
                                                          ├──→ activation ──→ validation ──→ soak
                                                          └──→ désactivation (rollback)
checklist : transverse, se remplit au fil des mesures
```

Deux règles d'ordre :

- La révision du relais **précède** la validation : mesurer la collecte avec
  l'ancien relais n'apprend rien sur les garde-fous du nouveau.
- La **référence initiale** précède toute mesure d'alerte : le premier cycle ne
  notifie rien, par conception.

## Découpage des responsabilités

Trois acteurs distincts, jamais confondus :

1. **Le dépôt** — code, tests, documentation. Ce qui est prouvé ici est prouvé
   pour le code, pas pour l'infrastructure.
2. **L'exploitation** — gestes sur le Pi, le VPS et le pare-feu, sous mandat
   d'exploitation. Aucun de ces gestes n'est décrit comme déjà fait.
3. **La mesure** — toute affirmation sur l'infrastructure doit être datée, attribuée
   à une commande et reportée même si elle contredit une attente. Une valeur non
   mesurée s'écrit **« à mesurer »**, jamais déduite d'une autre.

## Ordre imposé

```
mesures (lecture seule)
   → choix de variante (humain)
   → mandat d'exploitation (pare-feu / sshd / tunnel)
   → mise à jour du relais
   → validation du cycle réel
   → soak 48–72 h
   → décision d'activation (mandat séparé)
```

Aucune étape ne se saute. Les mesures précèdent le choix : choisir une variante
sans avoir mesuré le pare-feu revient à décider sans information.

## Opérations exigeant un mandat séparé

Chacune de ces opérations exige une autorisation explicite, nominative et datée,
**distincte** de ce dossier et distincte l'une de l'autre. Un même mandat ne
couvre jamais deux lignes.

| # | Opération | Pourquoi un mandat séparé |
| --- | --- | --- |
| M1 | Se connecter en SSH au Pi ou au VPS | Accès à l'infrastructure de production |
| M2 | Lire les `.env` réels ou tout secret | Risque d'exposition de secrets |
| M3 | Modifier une règle de pare-feu (`INPUT`, `DOCKER-USER`, `FORWARD`) | Peut élargir ou couper l'exposition réseau de l'hôte |
| M4 | Modifier la configuration `sshd` | Peut élargir les redirections autorisées |
| M5 | Modifier les restrictions de la clé du Pi | Change les capacités du compte |
| M6 | Ouvrir, fermer ou reconfigurer le tunnel | Coupe ou rétablit la sortie résidentielle |
| M7 | Redémarrer, arrêter ou démarrer le relais | Interrompt le service |
| M8 | Déployer, renommer ou remplacer le fichier du relais | Change le code en production |
| M9 | Modifier l'URL de proxy ou les options du collecteur | Change le chemin réseau réel de la collecte |
| M10 | Activer ou désactiver le collecteur | Change l'état du produit |
| M11 | Exécuter une requête non strictement en lecture seule | Effet sur les données ou sur la marketplace |
| M12 | Lancer un soak ou une fenêtre d'observation | Consomme la ressource et engage l'observation |
| M13 | Nettoyer branches, worktrees ou ressources | Perte de travail possible |

Les mandats M1 à M9 concernent l'exploitation ; M10 à M12 concernent la
validation ; M13 est hors périmètre de ce dossier.

## Suppression différée de l'ancienne clé

L'ancienne clé du Pi a été **désactivée, pas supprimée** : sa ligne reste dans
`authorized_keys` côté VPS, commentée avec la date et le motif. Elle n'accorde
donc plus aucun accès, et sa restauration reste possible en décommentant une
ligne.

Motif de la désactivation : la clé de tunnel est désormais **dédiée**, et son
entrée porte les restrictions qui bornent ce que le compte de tunnel peut faire.

Suppression **différée de 24 à 48 h**, sous mandat M5, quand les trois conditions
sont réunies :

1. la collecte a tourné un cycle complet avec la clé dédiée ;
2. le soak n'a montré aucune reconnexion manquée du tunnel ;
3. aucune reprise n'a eu à utiliser l'ancienne clé.

La copie de sauvegarde de `authorized_keys` prise avant modification reste la
référence de retour. Supprimer la ligne **et** la copie avant ces trois
conditions retirerait le filet sans avoir prouvé que le nouveau tient.

## Risques résiduels

Ce que les mesures ont établi, et ce qui reste ouvert :

| Risque | État | Conséquence |
| --- | --- | --- |
| Sortie résidentielle non prouvée | **Ouvert** | La jonction répond, mais aucune requête réelle vers la place de marché n'a été faite : la sortie pourrait être bloquée |
| Résolution DNS non bornée dans le relais | **Ouvert** | Huit résolutions bloquées saturent le relais ; la sonde le redétecte et le répare, avec le délai de `flock` |
| Passerelle réseau non épinglée en production | **Traité au dépôt, non déployé** | Sans le déploiement de l'épinglage, une recréation du réseau casse la jonction |
| AOF Redis non permanent | **Traité au dépôt, non déployé** | Sans `command:`, la clé d'alerte de silence ne survit pas à un redémarrage |
| Rotation de la clé d'administration du VPS | **Ouvert** | Non traitée par ce dossier |
| Secret historique sur disque | **Non qualifié** | Candidat repéré, non confirmé ; ne pas présumer son absence |
| Latence de réception utilisateur | **Non mesurable** | Le produit mesure `notified_at`, pas la réception |
| Restauration du volume Redis | **Non testée** | Le rollback existe, il n'a jamais été exercé |

Ces lignes ne sont pas des cases à cocher : elles disent ce qu'un soak peut encore
révéler, et ce qu'aucun test de dépôt ne révélera.

## Rappel : ce que ce dossier ne donne pas

Aucune de ces lignes n'est « pré-autorisée » par la lecture du dossier. Lire un
document n'est jamais exécuter une opération, et écrire une procédure ne crée
aucune permission.
