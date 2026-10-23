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
| 4 | Relais : écoute locale, refus hors loopback par défaut | ☐ Écoute constatée sur l'adresse attendue (mesure 4.2) — date : |
| 5 | Relais : validation stricte des domaines au démarrage | ☐ Ligne de commande relue, options valides — date : |
| 6 | Relais : backstop d'échéance testé | ☐ Résolveur système sain, pas de résolution pendante observée — date : |
| 7 | Relais : tests sur sockets simulées | ☐ Comportement confirmé sur réseau réel (doc. relais §4) — date : |
| 8 | Collecteur : exception périmètre (ADR-054) | ☐ Périmètre relu et confirmé applicable — date : |
| 9 | Collecteur : arrêt automatique sur blocage durable | ☐ Seuil d'arrêt validé pour l'exploitation — date : |
| 10 | Collecteur : collecte en processus, sans appel HTTP local | ☐ Ordre vérifié une fois en conditions réelles (annonce → matching → envoi, puis enrichissements en tâche de fond) — date : |
| 11 | Collecteur : référence initiale posée sans notification | ☐ Référence effectivement posée au premier cycle (doc. validation §0) — date : |
| 12 | Collecteur : alerte de structure de page inattendue (#160) | ☐ Alerte de structure vérifiée opérationnelle — date : |
| 13 | Jonction : problème conteneur → tunnel documenté | ☐ Mesures 1 à 5 complètes et datées — date : |
| 14 | Jonction : variantes décrites avec rollback | ☐ Variante **choisie** et consignée — date : |
| 15 | Jonction : rollback décrit | ☐ Rollback **testé au moins une fois** — date : |
| 16 | — | ☐ Pare-feu : ordre des chaînes connu (mesures 1.2–1.4) — date : |
| 17 | — | ☐ Pare-feu : persistance vérifiée (mesure 1.5) — date : |
| 18 | — | ☐ `sshd` : redirections, bind et `PermitListen` effectifs connus (mesures 3.1–3.2) — date : |
| 19 | — | ☐ Clé du Pi : noms d'options vérifiés (mesure 3.3) — date : |
| 20 | — | ☐ Tunnel : ouvert et stable sur une fenêtre observée — date : |
| 21 | — | ☐ Sortie résidentielle confirmée (mesure 4.5) — date : |
| 22 | — | ☐ Supervisabilité en place (doc. validation §3.2) — date : |
| 23 | — | ☐ Seuils d'échec du soak **confirmés** par écrit avant le soak (E2, E3, E8 sont des propositions) — date : |
| 24 | — | ☐ Fenêtre de soak planifiée (48–72 h) — date : |
| 25 | — | ☐ Responsable de la décision finale désigné — date : |
| 26 | — | ☐ Mandats séparés obtenus pour chaque opération concernée — date : |
| 27 | — | ☐ Scénario « tunnel tombé » exécuté (doc. validation §4) — date : |
| 28 | — | ☐ **Persistance Redis vérifiée** : support effectif du redémarrage pour l'épisode de blocage — date : |
| 29 | — | ☐ **Reprise manuelle testée au moins une fois** sous mandat (annonces non notifiées reprises) — date : |
| 30 | — | ☐ **IP de l'hôte dans l'historique Git** : décision prise (accepter et documenter, ou mandat de réécriture séparé) — date : |
| 31 | — | ☐ **Candidat de secret historique** qualifié (présent ou non, puis neutralisé ou accepté) — date : |
| 32 | — | ☐ **Schéma du proxy en `socks5h://`** vers la gateway du réseau applicatif (jamais `socks5://`, jamais `127.0.0.1`) — date : |
| 33 | — | ☐ **Dérive de la passerelle** : détectée par `scripts/check_junction_topology.py` lors du prochain contrôle, et règle « jamais de `docker compose down` sans le document » connue de l'exploitant — date : |
| 34 | — | ☐ **Jonction vérifiée** par `scripts/check_junction_topology.py` : conforme (ni en écart, ni non vérifiée) — date : |
| 35 | — | ☐ **Fenêtre de persistance Redis** exécutée ou explicitement reportée (doc. fenêtre) — date : |
| 36 | — | ☐ **Supervision par `flock`** constatée : une seule instance relais et tunnel, `@reboot` et `*/5` en place — date : |
| 37 | — | ☐ **Sonde de santé** exécutée pour de vrai, journal minimal et plafonné, aucun démarrage de relais par la sonde — date : |
| 38 | — | ☐ **Ancienne clé du Pi supprimée** après 24–48 h sans incident (désactivée aujourd'hui) — date : |

## Précisions sur trois lignes sensibles

### Ligne 10 — « aucun appel externe avant l'envoi Telegram »

Cette ligne a été reformulée. La formulation initiale affirmait une propriété
d'ordonnancement que **la seule lecture du code ne suffit pas** à attester en
conditions réelles, et elle laissait croire à une vérification exhaustive.

Ce qui est établi par le code : la collecte s'exécute **dans le processus
applicatif**, sans appel HTTP local intermédiaire, puis l'ingestion ouvre sa
session et traite l'annonce. Ce qui n'est pas établi par le code seul : la
séquence effective **en production**, mesurée une fois. D'où une ligne de dépôt
factuelle (« collecte en processus, sans appel HTTP local ») et un prérequis
opérationnel (« ordre vérifié une fois en conditions réelles »).

### Ligne 28 — persistance Redis

L'arrêt durable sur blocage s'appuie sur une clé Redis volontairement **sans
expiration**. Ce choix ne protège pas contre une **perte du contenu Redis** : si
le volume ne restitue pas la clé après un redémarrage, l'arrêt de sécurité est
levé de fait, sans décision humaine.

Le service Redis est déclaré avec un volume nommé et **sans réglage explicite de
persistance** ; le comportement de sauvegarde dépend donc des valeurs par défaut
de l'image. C'est un **à mesurer** : vérifier que la clé survit à un redémarrage
du service, sous mandat, et documenter le résultat.

### Lignes 30 et 31 — historique Git

Une adresse d'hôte a existé dans l'historique Git et en a été retirée. Tant que
l'historique n'est pas réécrit, elle reste accessible. Deux décisions possibles,
toutes deux explicites :

| Option | Conséquence |
| --- | --- |
| Accepter et documenter | L'historique conserve la valeur ; elle est jugée non sensible, ou suffisamment ancienne ; la décision est écrite et datée |
| Réécriture de l'historique | **Mandat séparé** obligatoire ; opération lourde et non réversible pour les clones existants |

La même alternative s'applique à tout **candidat de secret** repéré dans
l'historique : le qualifier d'abord (secret réel ou valeur inerte), puis choisir.
Un candidat non qualifié n'est ni « à risque » ni « sûr » : il est **inconnu**, et
cette ligne reste non cochée.

Aucune de ces deux lignes ne se coche « par défaut ». Ce sont des décisions.

## Ce que cette checklist ne fait pas

- Elle ne remplace pas les [mesures](leboncoin-junction-measurements.md).
- Elle ne remplace pas la [matrice de choix](leboncoin-junction-matrix.md).
- Elle ne remplace ni le [protocole de validation](leboncoin-activation-validation.md),
  ni la [procédure de mise à jour du relais](leboncoin-relay-update.md),
  ni la [procédure d'activation et de désactivation](leboncoin-activation-and-rollback.md).
- Elle **n'autorise aucune activation**. L'activation est une décision humaine,
  sous mandat distinct, après examen des preuves.
