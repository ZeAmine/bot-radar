# Collecte Leboncoin — activation et désactivation

Statut : procédure opérationnelle, **exécutable uniquement sous mandat explicite**.
Aucune commande n'est exécutée par la rédaction de ce document. Une case cochée
dans la [checklist](leboncoin-activation-checklist.md) ne vaut pas activation.

Périmètre : basculer la collecte Leboncoin de l'état désactivé à l'état actif, et
revenir en arrière par désactivation.

## État par défaut

La collecte est **désactivée par défaut**. Deux réglages la gouvernent :

| Variable | Valeur par défaut | Rôle |
| --- | --- | --- |
| `LEBONCOIN_POLLER_ENABLED` | `false` | Démarre ou non la tâche de collecte au lancement |
| `LEBONCOIN_PROXY_URL` | non défini | Sortie par le relais |
| `LEBONCOIN_PROXY_ENABLED` | `false` | Active l'usage du proxy |
| `LEBONCOIN_POLL_INTERVAL_SECONDS` | `600` | Cadence, conformément à ADR-053 |
| `LEBONCOIN_BLOCK_STOP_SECONDS` | `7200` | Seuil d'arrêt sur blocage durable |

La tâche de collecte n'est créée que si `LEBONCOIN_POLLER_ENABLED` est vrai. Une
valeur de proxy sans l'indicateur d'activation correspondant laisse la collecte
sans sortie résidentielle : les deux vont ensemble.

## Préalables — tous obligatoires

| # | Préalable | Preuve attendue |
| --- | --- | --- |
| P1 | Checklist complétée | Chaque ligne datée, y compris les lignes 30 et 31 |
| P2 | Variante de jonction choisie | Décision consignée avec les mesures qui la fondent |
| P3 | Relais à jour et vérifié | Journal de démarrage propre, tests de destinations faits |
| P4 | Rollback testé au moins une fois | Le retour a déjà été exécuté sur un environnement de test |
| P5 | Seuils du soak confirmés par écrit | E2, E3, E8 validés avant observation |
| P6 | Mandat d'activation obtenu | Autorisation explicite et datée |

**Aucun préalable ne se remplace par un autre.** Une checklist complète ne
dispense pas du mandat ; un mandat ne dispense pas des préalables techniques.

## 1. Activation

### 1.1 Préchecks avant modification

| # | Contrôle | Attend |
| --- | --- | --- |
| 1.1.1 | Service applicatif en bonne santé | Répond, sans erreur au démarrage |
| 1.1.2 | Relais joignable depuis le point d'entrée choisi | Réponse au salut SOCKS5 (mesure 4.3 ou 4.4) |
| 1.1.3 | Sortie résidentielle confirmée | Contrôle de sortie conforme (mesure 4.5) |
| 1.1.4 | Collecte actuellement inactive | Aucune tâche de collecte en cours, aucun cycle dans les journaux |
| 1.1.5 | Configuration actuelle relevée | Valeurs des variables ci-dessus notées **hors rapport** |

### 1.2 Modification

Ordre imposé — la sortie résidentielle d'abord, l'activation ensuite :

| # | Étape | Détail |
| --- | --- | --- |
| 1.2.1 | Renseigner `LEBONCOIN_PROXY_URL` | L'adresse atteignable **depuis le conteneur**, selon la variante retenue |
| 1.2.2 | Activer `LEBONCOIN_PROXY_ENABLED` | Le proxy devient effectif |
| 1.2.3 | Vérifier la cadence | `LEBONCOIN_POLL_INTERVAL_SECONDS` cohérent avec ADR-053 |
| 1.2.4 | Vérifier le seuil d'arrêt | `LEBONCOIN_BLOCK_STOP_SECONDS` validé par l'exploitation |
| 1.2.5 | Activer `LEBONCOIN_POLLER_ENABLED` **en dernier** | Seul ce réglage lance la collecte |
| 1.2.6 | Redémarrer le service applicatif | La configuration est lue au démarrage |

Activer le collecteur **avant** une sortie fonctionnelle produit des cycles en
échec et une alerte de silence. L'ordre n'est pas une commodité.

### 1.3 Vérifications immédiates

| # | Contrôle | Attend |
| --- | --- | --- |
| 1.3.1 | Démarrage propre | Aucune erreur de configuration |
| 1.3.2 | Tâche de collecte démarrée | Trace du premier cycle |
| 1.3.3 | Premier cycle = référence | Référence posée **sans notification** |
| 1.3.4 | Aucune alerte de structure | Le parsing reconnaît la page |
| 1.3.5 | Aucune alerte de silence | Un cycle exploitable a abouti |
| 1.3.6 | Aucun refus de destination | La liste couvre les domaines réellement utilisés |

### 1.4 Observation initiale

Sur la fenêtre choisie (au minimum quelques cycles complets, puis les 48–72 h du
[protocole](leboncoin-activation-validation.md)) :

| # | Observation | Attend |
| --- | --- | --- |
| 1.4.1 | Cycles suivants | Traitement normal, nouvelles annonces détectées |
| 1.4.2 | Première notification réelle | Survient sur une annonce **publiée après** la référence |
| 1.4.3 | Latences | p50 et p95 reportés avec leur effectif |
| 1.4.4 | Journal du relais | Aucun refus, aucune échéance anormale |
| 1.4.5 | Épisode de blocage | Absent |

## 2. Désactivation et retour arrière

La désactivation est le retour arrière normal. Elle **n'efface aucune donnée** :
les annonces déjà collectées et les alertes déjà envoyées restent.

| # | Étape | Détail |
| --- | --- | --- |
| 2.1 | Mettre `LEBONCOIN_POLLER_ENABLED` à `false` | La tâche de collecte cesse d'être créée |
| 2.2 | Redémarrer le service applicatif | L'arrêt de la tâche est effectif au redémarrage |
| 2.3 | Constater l'arrêt | Aucun cycle nouveau dans les journaux |
| 2.4 | Décider du proxy | Conserver la sortie résidentielle ou la retirer : les deux sont possibles, aucune n'est obligatoire |
| 2.5 | Vérifier l'état revenu | Voir le tableau ci-dessous |
| 2.6 | Consigner | Motif, heure, décideur, données conservées |

Conserver le proxy après désactivation est sans effet : aucune requête n'est
émise. Le retirer en plus coupe la dépendance au tunnel, au prix d'une
reconfiguration supplémentaire au retry.

### 2.1 Vérification que l'état est revenu

| # | Contrôle | Attend |
| --- | --- | --- |
| V1 | Tâche de collecte | Absente |
| V2 | Journaux | Plus aucun cycle nouveau |
| V3 | Alertes | Plus aucune alerte de collecte émise |
| V4 | Service applicatif | Toujours en bonne santé, autres fonctions intactes |
| V5 | Relais et tunnel | Inchangés par la désactivation, si conservés |
| V6 | Données | Annonces et alertes antérieures présentes |
| V7 | Configuration de retour | Valeurs identiques à celles relevées en 1.1.5, ou écart documenté |

**La désactivation n'est complète que quand V1 à V7 sont constatés.** Un simple
changement de variable sans redémarrage ne désactive rien.

## 3. Ce que cette procédure ne décide pas

- Elle ne choisit **pas** la variante de jonction : voir la
  [matrice](leboncoin-junction-matrix.md).
- Elle ne fixe **pas** les seuils du soak : voir le
  [protocole](leboncoin-activation-validation.md).
- Elle ne modifie **aucun** secret, aucune règle de pare-feu, aucune
  configuration `sshd`.
- Elle n'autorise **rien** : chaque étape s'exécute sous un mandat explicite et
  daté.
