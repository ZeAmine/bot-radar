# Collecte Leboncoin — protocole de validation après activation

Statut : protocole d'observation. Il suppose une activation **déjà autorisée et
réalisée sous mandat distinct**. Il ne déclenche aucune activation.

Objectif : décider, sur preuves mesurées, si la collecte tient ses promesses ou
doit être arrêtée. Chaque section produit des chiffres reportables.

## Principe de mesure

- **Lecture seule.** Toutes les mesures se lisent dans les journaux, la base et
  les métadonnées de messages. Aucune requête vers la marketplace n'est ajoutée
  par le protocole lui-même.
- **Dénominateur explicite.** Un taux n'a de sens qu'avec son dénominateur :
  « 12 alertes » ne dit rien sans « sur combien d'annonces retenues ».
- **Exclusion justifiée une par une.** Une annonce écartée d'une mesure est listée
  avec sa raison. Aucun filtrage silencieux.
- **Horodatage.** Chaque tableau porte la fenêtre observée.

## 1. Cycle réel annonce → matching → Telegram

Un cycle de référence, observé de bout en bout.

| # | Étape | Ce qui est observé | Dénominateur |
| --- | --- | --- | --- |
| 1.1 | Collecte | Pages demandées, annonces extraites | Annonces présentes sur la page |
| 1.2 | Dédoublonnage | Nouvelles annonces retenues | Annonces extraites |
| 1.3 | Matching | Annonces retenues par le matching | Nouvelles annonces |
| 1.4 | Envoi | Alertes envoyées | Annonces retenues |
| 1.5 | Réception | Messages effectivement reçus | Alertes envoyées |

**Exclusions à documenter** : annonce hors périmètre BD/manga, annonce déjà vue,
annonce sans ISBN exploitable, annonce écartée par un filtre de prix, échec
d'enrichissement non bloquant. Chacune est comptée **séparément** avec sa raison ;
aucune n'est agrégée dans un « divers ».

Le cycle est **conforme** si chaque étape a un dénominateur non nul et si le
rapport entre étapes est expliqué. Un trou entre 1.2 et 1.3 sans explication est un
défaut, pas une mesure.

## 2. Latences

Deux latences distinctes, à ne pas confondre.

### 2.1 Publication → réception

Durée entre l'horodatage de publication de l'annonce et la réception du message.

| Métrique | Définition | Source |
| --- | --- | --- |
| Médiane | 50ᵉ centile des délais observés | Horodatages annonce et message |
| 90ᵉ centile | 90ᵉ centile | Idem |
| Maximum | Le pire cas de la fenêtre | Idem |
| Effectif | Nombre d'alertes mesurées | Dénominateur |

**Exclusions** : annonce dont l'horodatage de publication manque, annonce reprise
lors d'un rattrapage après interruption. Chacune est listée et comptée.

### 2.2 Réception → notification

Durée entre la réception du message et la notification effective côté client.

| Métrique | Définition |
| --- | --- |
| Médiane / 90ᵉ centile / maximum | Mêmes définitions |
| Effectif | Nombre de notifications mesurées |

**Requêtes en lecture seule uniquement** : les métadonnées de messages et les
journaux. Aucun envoi de test supplémentaire n'est réalisé pour mesurer cette
latence — cela fausserait la mesure et solliciterait l'utilisateur inutilement.

## 3. Soak 48 à 72 heures

Fenêtre minimale 48 h, cible 72 h. Aucune conclusion avant 48 h complètes.

### 3.1 Critères d'échec — arrêt immédiat

Le soak est **échoué** dès que l'un de ces événements survient :

| # | Critère | Seuil |
| --- | --- | --- |
| E1 | Arrêt sur blocage durable déclenché | Un seul épisode suffit |
| E2 | Cycles consécutifs sans aucune page exploitable | À définir avant le soak, puis figé |
| E3 | Tunnel interrompu et non rétabli | Au-delà de la fenêtre d'observation choisie |
| E4 | Refus de destination sur un domaine nécessaire | Un seul refus « manquant » suffit |
| E5 | Relais ayant atteint le plafond de connexions de façon répétée | Plusieurs occurrences par heure |
| E6 | Résolution DNS bloquée retenant durablement un worker | Persistance au-delà de la fenêtre choisie |
| E7 | Erreur de collecte non expliquée et répétée | Selon la fréquence observée |
| E8 | Écart de latence au-delà du seuil choisi avant le soak | Un seul dépassement franc et répété |

**Règle** : les seuils E2, E3 et E8 sont **fixés avant** le début du soak, par
écrit. Un seuil ajusté pendant le soak transforme la mesure en auto-justification.

### 3.2 Supervision à mettre en place

| # | Élément supervisé | Signal observé | Fréquence |
| --- | --- | --- | --- |
| S1 | Relais vivant | Processus présent | Horaire |
| S2 | Tunnel ouvert | Processus de tunnel présent | Horaire |
| S3 | Journaux du relais | Absence de refus et d'échéance anormale | Horaire |
| S4 | Journaux du collecteur | Cycles réussis, erreurs | Horaire |
| S5 | Épisode de blocage | Absence d'épisode ouvert | Horaire |
| S6 | Sortie résidentielle | Contrôle de sortie périodique | Quotidien |
| S7 | Espace disque | Marge disponible | Quotidien |

Cette supervision est **manuelle ou outillée**, mais elle doit exister : un soak
sans supervision ne produit qu'une absence de nouvelles.

### 3.3 Tableau quotidien

À remplir chaque jour, même si tout va bien.

| Jour | Cycles prévus | Cycles réussis | Pages exploitées | Annonces extraites | Nouvelles | Alertes envoyées | Épisodes de blocage | Tunnel interrompu | Refus de destination | Plafond atteint | Latence médiane | Latence max | Incident |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| J1 | | | | | | | | | | | | | |
| J2 | | | | | | | | | | | | | |
| J3 | | | | | | | | | | | | | |

Une cellule vide est une **mesure manquante**, pas un zéro. Elle réduit la
confiance dans la conclusion.

### 3.4 Conclusion du soak

| Résultat | Condition |
| --- | --- |
| Réussi | Aucun critère E1–E8 déclenché, mesures complètes sur la fenêtre |
| Réussi avec réserves | Aucun critère E1–E8, mais des cellules manquantes — à lister |
| Échoué | Au moins un critère déclenché — l'arrêt ou la correction est la suite |

## 4. Scénario volontaire « tunnel tombé »

Objectif : vérifier que l'échec est **visible**, pas silencieux.

| # | Étape | Attend |
| --- | --- | --- |
| 4.1 | Couper le tunnel depuis le Pi | Tunnel absent |
| 4.2 | Attendre le cycle suivant | Erreur de collecte explicite dans les journaux |
| 4.3 | Observer l'alerte | Une alerte après le nombre de cycles prévu, pas d'alerte immédiate |
| 4.4 | Rétablir le tunnel | Tunnel présent |
| 4.5 | Observer la reprise | Cycle réussi, sortie résidentielle confirmée |
| 4.6 | Vérifier l'absence de doublon | Aucune alerte dupliquée après reprise |

Échoue si la panne est invisible, si l'alerte survient trop tôt ou trop tard, ou
si la reprise produit des doublons.

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
modifier le périmètre pour « passer » un blocage. La collecte bénéficie d'une
exception périmètre précis (ADR-054) ; cette exception ne couvre aucun
contournement.

## 6. Décision finale

| Résultat du soak | Suite |
| --- | --- |
| Réussi | Poursuite nominale, supervision maintenue |
| Réussi avec réserves | Poursuite **avec** la liste des mesures manquantes à compléter |
| Échoué | Retour à l'état désactivé, analyse, correction, nouveau protocole |

La décision est prise par l'humain, consignée avec sa date, et n'est jamais
déduite d'une absence de nouvelles.
