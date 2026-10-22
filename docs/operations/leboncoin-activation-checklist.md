# Collecte Leboncoin — checklist d'activation

Statut : checklist de préparation. **Aucune case cochée ne vaut activation.**
Cocher une case atteste seulement qu'un élément a été vérifié, à la date indiquée.

Structure en deux colonnes :

- **Fait côté dépôt** : ce qui est livré, testé et versionné ici.
- **Prérequis opérationnels** : ce qui ne peut être vérifié que par une mesure
  datée, sur l'infrastructure réelle.

Le déséquilibre est volontaire : la colonne dépôt est courte et vérifiable ; la
colonne opérationnelle est longue et datée.

## Tableau

| # | Fait côté dépôt | Prérequis opérationnel (mesure datée) |
| --- | --- | --- |
| 1 | Relais : liste de destinations autorisées, refus explicite | ☐ Liste des destinations réellement nécessaires établie (doc. relais §4) — date : |
| 2 | Relais : plafond de connexions simultanées | ☐ Plafond adapté à la charge observée — date : |
| 3 | Relais : durée maximale par connexion | ☐ Aucune connexion légitime coupée observée — date : |
| 4 | Relais : écoute locale, refus hors loopback par défaut | ☐ Écoute constatée sur l'adresse attendue (mesure 5.2) — date : |
| 5 | Relais : validation stricte des domaines au démarrage | ☐ Ligne de commande relue, options valides — date : |
| 6 | Relais : backstop d'échéance testé | ☐ Résolveur système sain, pas de résolution pendante observée — date : |
| 7 | Relais : tests sur sockets simulées | ☐ Comportement confirmé sur réseau réel (doc. relais §4) — date : |
| 8 | Collecteur : exception périmètre (ADR-054) | ☐ Périmètre relu et confirmé applicable — date : |
| 9 | Collecteur : arrêt automatique sur blocage durable | ☐ Seuil d'arrêt validé pour l'exploitation — date : |
| 10 | Alerte : aucun appel externe avant l'envoi Telegram | ☐ Ordre vérifié en conditions réelles — date : |
| 11 | Jonction : problème conteneur → tunnel documenté | ☐ Mesures 1 à 5 complètes et datées — date : |
| 12 | Jonction : variantes décrites avec rollback | ☐ Variante **choisie** et consignée — date : |
| 13 | Jonction : rollback décrit | ☐ Rollback **testé au moins une fois** — date : |
| 14 | — | ☐ Pare-feu : ordre des chaînes connu (mesures 1.2–1.4) — date : |
| 15 | — | ☐ Pare-feu : persistance vérifiée (mesure 1.5) — date : |
| 16 | — | ☐ `sshd` : redirections et bind effectifs connus (mesures 3.1–3.5) — date : |
| 17 | — | ☐ Clé du Pi : restrictions vérifiées (mesures 4.1–4.3) — date : |
| 18 | — | ☐ Tunnel : ouvert et stable sur une fenêtre observée — date : |
| 19 | — | ☐ Sortie résidentielle confirmée (mesure 5.6) — date : |
| 20 | — | ☐ Supervisabilité en place (doc. validation §3.2) — date : |
| 21 | — | ☐ Seuils d'échec du soak fixés par écrit **avant** le soak — date : |
| 22 | — | ☐ Fenêtre de soak planifiée (48–72 h) — date : |
| 23 | — | ☐ Responsable de la décision finale désigné — date : |
| 24 | — | ☐ Mandats séparés obtenus pour chaque opération concernée — date : |
| 25 | — | ☐ Scénario « tunnel tombé » exécuté (doc. validation §4) — date : |

## Règles d'usage

- Une case sans date est une case **non faite**.
- Une case cochée sur la foi d'une déduction, sans mesure, est une **fausse preuve** :
  la décocher.
- La colonne « dépôt » ne prouve rien de l'infrastructure. Elle indique seulement
  que l'outil existe et qu'il est testé dans le dépôt.
- Les lignes 14 à 25 n'ont volontairement **aucun** équivalent dépôt : elles ne
  dépendent d'aucun livrable logiciel.

## Ce que cette checklist ne fait pas

- Elle ne remplace pas les [mesures](leboncoin-junction-measurements.md).
- Elle ne remplace pas la [matrice de choix](leboncoin-junction-matrix.md).
- Elle ne remplace ni le [protocole de validation](leboncoin-activation-validation.md),
  ni la [procédure de mise à jour du relais](leboncoin-relay-update.md).
- Elle **n'autorise aucune activation**. L'activation est une décision humaine,
  sous mandat distinct, après examen des preuves.
