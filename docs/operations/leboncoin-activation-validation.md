# Collecte Leboncoin — protocole de validation après activation

Statut : protocole d'observation. Il suppose une activation **déjà autorisée et
réalisée sous mandat distinct**. Il ne déclenche aucune activation.

Objectif : décider, sur preuves mesurées, si la collecte tient ses promesses ou
doit être arrêtée. Chaque section produit des chiffres reportables.

## Principe de mesure

- **Lecture seule.** Toutes les mesures se lisent dans la base et les journaux.
  Aucune requête vers la marketplace n'est ajoutée par le protocole.
- **Dénominateur explicite.** Un taux n'a de sens qu'avec son dénominateur.
- **Exclusion justifiée une par une.** Une mesure écartée est listée avec sa raison.
- **Horodatage.** Chaque tableau porte la fenêtre observée.

## 0. Référence initiale (baseline)

**Le premier cycle ne notifie rien.** À la première exécution, le poller marque
toutes les annonces de la page comme déjà vues, sans en notifier aucune
(`baseline_established`). C'est un comportement voulu : sans cette référence, la
première collecte enverrait d'un coup toute la page.

Conséquences pour la mesure :

- Le premier cycle **pose la référence** et ne compte **pas** comme un cycle de
  notification. Il ne produit ni annonce nouvelle, ni alerte.
- Le test « annonce → matching → Telegram » de la section 2 ne peut donc se faire
  qu'**après** la baseline, sur une annonce **publiée après** elle.
- Une annonce publiée **avant** la baseline ne déclenchera jamais d'alerte : elle
  est dans la référence. Ces annonces ne sont **pas** un défaut de collecte et ne
  doivent pas être comptées comme manquées.

| # | Contrôle | Attend |
| --- | --- | --- |
| 0.1 | Référence posée | Journal mentionnant la référence posée, nombre d'annonces marquées vues |
| 0.2 | Aucune notification au premier cycle | Zéro message Telegram pour ce cycle |
| 0.3 | Cycle suivant | Traitement normal des nouvelles annonces |

## 1. Cycle réel annonce → matching → Telegram

Un cycle de référence, observé de bout en bout, **après** une baseline établie.

| # | Étape | Ce qui est observé | Dénominateur |
| --- | --- | --- | --- |
| 1.1 | Collecte | Pages demandées, annonces extraites | Annonces présentes sur la page |
| 1.2 | Dédoublonnage | Nouvelles annonces retenues | Annonces extraites |
| 1.3 | Matching | Annonces retenues par le matching | Nouvelles annonces |
| 1.4 | Envoi | Alertes envoyées | Annonces retenues |
| 1.5 | Réception | Messages effectivement reçus | Alertes envoyées |

**Exclusions à documenter, une par une** :

| Exclusion | Justification |
| --- | --- |
| Annonce hors périmètre BD/manga | Le filtre de recherche est le périmètre produit |
| Annonce déjà vue (référence ou cycle antérieur) | Le dédoublonnage est le comportement attendu |
| Annonce sans ISBN exploitable | Le matching ne peut pas conclure |
| Annonce écartée par un filtre de prix | Décision produit explicite |
| Échec d'enrichissement non bloquant | L'alerte part sans l'enrichissement, par conception |

Chacune est comptée **séparément**. Aucune n'est agrégée dans un « divers » : une
exclusion dont on ne sait pas nommer la cause est un **défaut**, pas une exclusion.

Le cycle est **conforme** si chaque étape a un dénominateur non nul et si l'écart
entre étapes est expliqué par une exclusion nommée. Un trou entre 1.2 et 1.3 sans
exclusion nommée est un défaut.

## 2. Latences — sémantique exacte

Les deux latences mesurables se calculent sur des colonnes **distinctes**. Les
confondre produit un chiffre faux.

### 2.1 Publication → collecte

```
received_at − published_at
```

| Élément | Détail |
| --- | --- |
| `published_at` | Horodatage de publication fourni par la source |
| `received_at` | Horodatage de persistance de l'annonce, à la collecte |
| Ce que cela mesure | Le délai de **découverte** : temps entre la mise en ligne et la persistance locale |
| Ce que cela ne mesure pas | Ni le matching, ni l'envoi Telegram |

`published_at` n'est disponible que pour Leboncoin : cette latence n'est pas
comparable aux sources qui ne le fournissent pas.

### 2.2 Collecte → envoi acquitté

```
notified_at − received_at
```

| Élément | Détail |
| --- | --- |
| `received_at` | Horodatage de persistance de l'annonce |
| `notified_at` | Horodatage posé **après l'acquittement d'envoi** et **avant le commit** |
| Ce que cela mesure | Un **majorant** du délai matching → envoi accepté |
| Ce que cela ne mesure pas | La réception effective par l'utilisateur |

**`notified_at` est un majorant, pas la réception.** L'horodatage est posé avant
le commit de la transaction : le délai réel jusqu'à l'utilisateur est **inférieur
ou égal** à `notified_at − received_at`. L'écart inclut le temps de commit.

**La réception effective côté utilisateur n'est pas mesurable depuis la base.**
Aucun accusé de lecture du client Telegram n'est persisté. Toute affirmation sur
« le temps avant que l'utilisateur le voie » serait une extrapolation, pas une
mesure. Le protocole ne la revendique pas.

### 2.3 Cible et comparaison

La cible est **p95 ≤ 10 min** sur la latence mesurée depuis la publication
(ADR-053, seuils pré-enregistrés).

| Métrique | Définition | Cible |
| --- | --- | --- |
| p50 | Médiane des délais | — |
| **p95** | 95ᵉ centile — **la valeur comparée à la cible** | **≤ 10 min** |
| Maximum | Pire cas de la fenêtre | — |
| Effectif | Nombre d'alertes mesurées | ≥ 20 pour conclure |

**Cette cible n'est pas revendiquée ici.** Elle est comparée après mesure, avec
son effectif. En dessous de 20 mesures, le p95 n'est pas une statistique stable :
il est reporté comme **indicatif**, pas comme atteinte ou non atteinte de la cible.

Quelle latence comparer à la cible : la latence **publication → envoi acquitté**,
c'est-à-dire `notified_at − published_at`. Elle se décompose en 2.1 + 2.2, ce qui
permet de savoir **quelle étape** coûte le délai. Les deux composantes sont
toujours reportées séparément, sinon un dépassement ne dit pas où corriger.

### 2.4 Requêtes en lecture seule

Les mesures se lisent dans la base et les journaux. Aucun envoi de test n'est
réalisé pour mesurer une latence : cela fausserait la mesure et solliciterait
l'utilisateur inutilement.

## 3. Soak 48 à 72 heures

Fenêtre minimale 48 h, cible 72 h. Aucune conclusion avant 48 h complètes.

### 3.1 Critères d'échec

> **Les seuils chiffrés ci-dessous sont des PROPOSITIONS À CONFIRMER avant le
> soak. Ils ne sont pas des valeurs actives.** Ils doivent être validés par écrit
> par le responsable de la décision, puis figés. Un seuil ajusté pendant le soak
> transforme la mesure en auto-justification.

| # | Critère | Seuil proposé — **à confirmer** |
| --- | --- | --- |
| E1 | Arrêt sur blocage durable déclenché | Un seul épisode suffit |
| E2 | Cycles consécutifs sans aucune page exploitable | **5 cycles consécutifs** |
| E3 | Tunnel non rétabli (hors scénario volontaire) | **30 min** |
| E4 | Refus de destination sur un domaine nécessaire | Un seul refus « manquant » suffit |
| E5 | Plafond de connexions atteint de façon répétée | Plusieurs occurrences par heure |
| E6 | Résolution DNS bloquée retenant durablement un worker | Persistance au-delà de la fenêtre choisie |
| E7 | Erreur de collecte non expliquée et répétée | Selon la fréquence observée |
| E8 | p95 publication → envoi acquitté | **> 10 min sur 24 h avec au moins 20 alertes mesurées** |
| E9 | **Alerte de structure de page non résolue** | Un seul déclenchement suffit |
| E10 | **Alerte de silence non expliquée** | Un seul déclenchement suffit |

**E9** : l'alerte « structure de page inattendue » signale que le parsing ne
reconnaît plus le format du site. Elle est traitée comme un échec, jamais comme un
bruit : un parsing dégradé produit des cycles « réussis » sans annonces.

**E10** : l'alerte de silence prévient qu'aucun cycle exploitable n'a abouti
depuis le seuil. Elle doit être **expliquée** (tunnel, blocage, maintenance
planifiée). Une alerte de silence sans cause identifiée est un échec.

Note sur E8 : le seuil proposé reprend la cible ADR-053 (p95 ≤ 10 min) sur une
fenêtre de 24 h avec un effectif minimal de 20 alertes. Il est exprimé sur la
latence **publication → envoi acquitté**, donc déjà majorée par 2.2 : il est
plutôt conservateur.

### 3.2 Supervision à mettre en place

| # | Élément supervisé | Signal observé | Fréquence |
| --- | --- | --- | --- |
| S1 | Relais vivant | Processus présent | Horaire |
| S2 | Tunnel ouvert | Processus de tunnel présent | Horaire |
| S3 | Journaux du relais | Absence de refus et d'échéance anormale | Horaire |
| S4 | Journaux du collecteur | Cycles réussis, erreurs | Horaire |
| S5 | Épisode de blocage | Absence d'épisode ouvert | Horaire |
| S6 | Alerte de structure de page | Absence de déclenchement non résolu | Quotidien |
| S7 | Alerte de silence | Absence, ou cause expliquée | Quotidien |
| S8 | Sortie résidentielle | Contrôle de sortie périodique | Quotidien |
| S9 | Espace disque | Marge disponible | Quotidien |

Un soak sans supervision ne produit qu'une absence de nouvelles.

### 3.3 Tableau quotidien

| Jour | Cycles prévus | Cycles réussis | Pages exploitées | Annonces extraites | Nouvelles | Alertes envoyées | Épisodes blocage | Tunnel interrompu | Refus destination | Plafond atteint | p50 pub→envoi | p95 pub→envoi | Alertes structure | Alertes silence | Incident |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| J1 | | | | | | | | | | | | | | | |
| J2 | | | | | | | | | | | | | | | |
| J3 | | | | | | | | | | | | | | | |

Une cellule vide est une **mesure manquante**, pas un zéro.

### 3.4 Conclusion du soak

| Résultat | Condition |
| --- | --- |
| Réussi | Aucun critère E1–E10 déclenché, mesures complètes |
| Réussi avec réserves | Aucun critère E1–E10, mais des cellules manquantes — à lister |
| Échoué | Au moins un critère déclenché |

## 4. Scénario volontaire « tunnel tombé »

| # | Étape | Attend |
| --- | --- | --- |
| 4.1 | Couper le tunnel depuis le Pi | Tunnel absent |
| 4.2 | Attendre le cycle suivant | Erreur de collecte explicite dans les journaux |
| 4.3 | Observer l'alerte | Alerte de silence après le nombre de cycles prévu, pas immédiate |
| 4.4 | Rétablir le tunnel | Tunnel présent |
| 4.5 | Observer la reprise | Cycle réussi, sortie résidentielle confirmée |
| 4.6 | Vérifier l'absence de doublon | Aucune alerte dupliquée après reprise |

Ce scénario est **exclu** du comptage E3 : la coupure est provoquée, pas subie.
Échoue si la panne est invisible, si l'alerte est mal calibrée, ou si la reprise
produit des doublons.

## 5. Blocage DataDome durable

Le comportement est décrit par la [procédure d'arrêt sur blocage](leboncoin-block-stop.md).
Cette section décrit la **décision humaine**, qui n'est pas automatisable.

| # | Étape | Détail |
| --- | --- | --- |
| 5.1 | Constater l'épisode | Premier refus daté, durée observée |
| 5.2 | Attendre le seuil | L'arrêt est automatique ; ne pas le devancer |
| 5.3 | Qualifier | Blocage bref déjà vu, ou situation nouvelle ? |
| 5.4 | Ne rien contourner | Aucun changement d'empreinte, de proxy ou de cadence n'est décidé seul |
| 5.5 | Décider | Reprise automatique après un cycle réussi, ou arrêt prolongé décidé explicitement |
| 5.6 | Consigner | Date, durée, décision, auteur de la décision |

**Interdit** : augmenter la cadence, changer d'empreinte, ajouter des proxys ou
modifier le périmètre pour « passer » un blocage. L'exception de périmètre
(ADR-054) ne couvre aucun contournement.

## 6. Décision finale

| Résultat du soak | Suite |
| --- | --- |
| Réussi | Poursuite nominale, supervision maintenue |
| Réussi avec réserves | Poursuite **avec** la liste des mesures manquantes |
| Échoué | Désactivation (voir la [procédure](leboncoin-activation-and-rollback.md)), analyse, correction |

La décision est prise par l'humain, consignée avec sa date, et n'est jamais
déduite d'une absence de nouvelles.
