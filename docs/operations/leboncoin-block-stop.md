# Collecte Leboncoin — arrêt sur blocage durable

Statut : procédure opérationnelle du collecteur Leboncoin (ADR-052, ADR-054).
Elle ne concerne que ce collecteur : Vinted, l'API, le bot et le planificateur
continuent de fonctionner quand le collecteur Leboncoin est arrêté.

## Ce qui se passe

Un refus DataDome (`403` ou page de challenge) ouvre un **épisode de blocage**.
L'épisode est daté au premier refus et n'est refermé que par un cycle de collecte
réussi. Les erreurs réseau et les erreurs de lecture de page n'ouvrent ni ne
referment l'épisode : elles laissent courir sa fenêtre.

Tant qu'un épisode est ouvert, toutes les attentes sont plafonnées au temps restant
avant la limite, et la limite est contrôlée **avant chaque nouvelle requête**.

## Seuil

`LEBONCOIN_BLOCK_STOP_SECONDS` (défaut `7200`, soit deux heures, valeur strictement
positive). Au-delà, le collecteur Leboncoin s'arrête sans émettre de nouvelle requête.

Cette durée est une **décision d'exploitation initiale**, pas une valeur démontrée
par la mesure disponible : le seul blocage mesuré a duré moins de 45 min.

Le compteur de refus consécutifs reste informatif : il choisit le palier de pause
et le texte des alertes, il ne décide pas de l'arrêt.

L'alerte d'arrêt annonce la **durée réellement observée** depuis le premier refus,
et rappelle le seuil configuré. Elle n'affirme donc pas une durée exacte de
blocage lorsqu'un redémarrage tardif constate l'échéance après coup.

Un cycle réussi n'annonce le rétablissement qu'**après** l'effacement effectif de
l'épisode : si l'effacement échoue, aucun rétablissement complet n'est annoncé.

## Ce qui est enregistré

L'état vit dans la clé Redis `radar:leboncoin_block_episode`, volontairement
**sans expiration** : un arrêt de sécurité ne doit pas se lever tout seul au bout
d'un délai.

Sans expiration ne veut pas dire indestructible. La clé vit dans Redis : une perte
du contenu de Redis (redémarrage sans persistance, `FLUSHALL`, volume remplacé)
efface aussi cet état, et l'arrêt n'est alors plus connu du processus suivant.
La durabilité de l'arrêt dépend donc de la persistance Redis, pas de l'absence de TTL.

## Arrêt et reprise

1. Le collecteur s'arrête, une alerte dédiée annonce la transition une seule fois.
2. `/status` affiche « Arrêté sur blocage durable (reprise manuelle) », ou
   « Arrêté (persistance impossible : reprise non garantie) » si l'arrêt n'a pas
   pu être enregistré.
3. Après un redémarrage du service, l'état d'arrêt est relu : le collecteur
   **ne redémarre pas** et l'alerte n'est pas rejouée.
4. La reprise est **manuelle** :

```sh
redis-cli -u "$REDIS_URL" DEL radar:leboncoin_block_episode
```

   puis redémarrage du service. Aucune commande Telegram ni API ne permet la reprise.

Ne pas supprimer cette clé sans avoir constaté que la collecte est de nouveau
acceptée : l'épisode ne sert qu'à empêcher une reprise automatique sur un site
qui refuse.

## États dégradés

- **État illisible ou invalide au démarrage** : le collecteur ne démarre pas et
  `/status` indique « Suspendu (état de blocage illisible) ». L'absence de clé,
  elle, est normale et laisse démarrer la collecte.
- **État impossible à écrire à l'ouverture d'un épisode** : le collecteur est
  suspendu pour le processus courant et `/status` indique
  « Arrêté (persistance impossible : reprise non garantie) ».
- **Arrêt déjà décidé mais impossible à enregistrer** : le collecteur s'arrête
  quand même, et le motif final reste `persistence_failed` — il n'est jamais
  remplacé par un arrêt correctement enregistré. L'alerte le dit explicitement.
  La garantie de non-reprise **après redémarrage n'est pas assurée** tant que
  l'écriture n'a pas abouti : après un redémarrage, le collecteur peut repartir.
  Vinted n'est pas concerné.
- **Un échec d'envoi Telegram ne modifie rien** : l'arrêt ou la suspension est
  déjà appliqué, il n'est ni différé ni annulé par l'échec de l'alerte. Une
  suspension donne lieu à **une** alerte dédiée, sans détail brut de Redis.

## Visibilité dans `/status`

Le statut courant expire au bout de 24 h. Une fois le collecteur arrêté, plus rien
ne le réécrit : `/status` relit donc aussi la clé d'épisode, qui n'expire pas.
Un arrêt enregistré reste ainsi visible au-delà de 24 h.

`/status` n'effectue aucune reprise : il se contente de lire. L'absence de clé
reste un état normal, et un état d'épisode illisible est affiché comme tel plutôt
que comme un état sain.

Un arrêt dont la persistance a échoué (`persistence_failed`) reste affiché depuis
le statut courant : cet arrêt n'a pas d'état persistant garanti, et cette
information prime sur l'état persisté.

## Hors périmètre

- L'absence d'alerte dédiée aux erreurs de lecture de page (`LeboncoinParseError`)
  reste un défaut connu, à traiter avant activation ; cette procédure ne le corrige pas.
- Le durcissement du relais résidentiel, les changements réseau et l'activation
  de la collecte ne sont pas couverts ici.
