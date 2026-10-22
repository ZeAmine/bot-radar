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
| [Mesures de jonction](leboncoin-junction-measurements.md) | Mesures en lecture seule, ce qu'elles prouvent, ce qu'il ne faut pas recopier |
| [Matrice de jonction](leboncoin-junction-matrix.md) | Variantes V1 / V2 / autres, risques, contrôles, rollback |
| [Mise à jour du relais](leboncoin-relay-update.md) | Préchecks, déploiement, vérification, rollback |
| [Validation après activation](leboncoin-activation-validation.md) | Cycle réel, latences, soak, tunnel tombé, blocage durable |
| [Checklist d'activation](leboncoin-activation-checklist.md) | Deux colonnes : dépôt / prérequis opérationnels |
| [Mandats requis](#opérations-exigeant-un-mandat-séparé) | Liste explicite des opérations hors de ce dossier |

Les cinq procédures ont les dépendances suivantes :

```
mesures ──→ matrice ──→ (mandat d'exploitation)
                            ├──→ mise à jour du relais ──→ validation ──→ soak
                            └───────────────────────────↗
checklist : transverse, se remplit au fil des mesures
```

La mise à jour du relais **précède** la validation : mesurer la collecte avec
l'ancien relais n'apprend rien sur les garde-fous du nouveau.

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

## Rappel : ce que ce dossier ne donne pas

Aucune de ces lignes n'est « pré-autorisée » par la lecture du dossier. Lire un
document n'est jamais exécuter une opération, et écrire une procédure ne crée
aucune permission.
