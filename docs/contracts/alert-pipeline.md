# Contrat d'alerte : ingestion, matching et notification

Statut : contrat actuel. Périmètre : annonce entrante jusqu'aux traitements de fond.
Règle transversale : [invariant 1](../../AGENTS.md#invariant-1--telegram-dabord).

## Ordre et frontières

Le [poller](../../app/tasks/vinted_poller.py) transforme le catalogue en charge
pour [l'ingestion](../../app/use_cases/ingest_listing.py). Le parsing Vinted reste
pur dans `vinted_parsing.py` ; le scraper porte la session et le réseau.
La couverture et l'identifiant vendeur sont transportés depuis le catalogue, sans
lecture d'article supplémentaire. La description synthétique n'est pas celle du vendeur.

1. Dédoublonner par `(source_name, external_id)` et persister l'annonce.
2. Évaluer les alertes par les [règles CPU](../../app/domain/rules/listing_match.py).
   La lecture groupée `match_notification_state` par annonce reste distincte du calcul.
3. Résoudre les livres déjà chargés et lire la cote en cache, sans attente HTTP.
4. Envoyer Telegram avec un bloc de cote ou une ligne décrivant son absence.
5. Après les envois, lancer via `safe_create_task` la liaison bibliographique dans
   sa session propre. La réception ne déclenche aucune collecte de prix : les
   livres liés rejoignent les lots du scheduler. L'IA est lancée depuis
   `telegram_service.py` avec le vrai identifiant du message accepté.

Le titre et l'ISBN validé partent ensemble à tous les fournisseurs dans la cotation
centrale, repli par titre compris. Cette collecte sert les annonces suivantes.

## Matching et état envoyé

L'état, la catégorie, le prix, l'identité ISBN, les exclusions, la langue et les
inclusions/variantes sont des règles locales. Les exclusions ont priorité.
Une variante est une autre écriture du même produit ; les nombres sont contrôlés
aussi en lecture. La garde de mot reconnaissable ne s'applique qu'à l'écriture,
pour ne pas rendre muettes des variantes déjà stockées. La politique détaillée est
expliquée dans [Telegram et IA](telegram-ai.md) et [le diagnostic](../matching-diagnostics.md).
Un score n'est pas une probabilité ni une certification bibliographique.

Le résumé marché synchrone ne collecte rien. À l'envoi : un livre sans relevé
utilisable porte `COTE_NOT_READY_LINE`, un lot non composé `LOT_UNRESOLVED_LINE`.
Une passe vide datée, une collecte échouée et un catalogue réellement épuisé restent
distincts. Aucun état n'annonce une future réécriture du message de cote.
Le [contrat marché](catalogue-market.md) précise leur signification.

L'accusé de transport, sa persistance et l'état final sont différents. Les réservations
durables et la reprise bornée sont définies dans
[notification-delivery](../notification-delivery.md) ; ne pas présenter une émission
incertaine comme non envoyée ou promettre une livraison exactement une fois.
`ProcessListingUseCase` utilise par défaut `send_listing_alert` ; les doubles
s'injectent par son paramètre typé `sender`, qui retourne `DeliveryResult`.

## Observabilité de la scrutation

Le [poller](../../app/tasks/vinted_poller.py) émet une ligne INFO par cycle via le
`JSONFormatter`, avec `event="poller_cycle"` : issue (`scanned`, `night`, `blocked`,
`network_error`, `unexpected_error`), durée, annonces traitées et requêtes Vinted
émises par type (`api`, `html`, `mint`). Ces compteurs réutilisent les requêtes
existantes : l'observabilité n'ajoute aucun appel réseau. La cadence ne subit aucune
pause aléatoire ajoutée ; chaque entrée et sortie de la veille de nuit laisse une
ligne `event="poller_night"` (`phase="start"` avec `remaining_s`, `phase="end"` avec
`duration_s`). Chaque lecture de catalogue laisse aussi sa ligne `catalogue_read`,
décrite dans [les frontières de latence](../alert-latency.md#lecture-de-catalogue).

Le proxy Vinted n'est utilisé que si `VINTED_PROXY_ENABLED` est vrai : une
`VINTED_PROXY_URL` seule n'active ni le proxy des clients, ni la cadence accélérée ;
un avertissement unique la signale au démarrage.

La commande `/status` rend ces champs quand l'état Redis les porte :

- « Disponibilité scan » : part du temps couvert par les cycles passée en scrutation,
  avec le détail bloqué / nuit / réseau / erreurs. La fenêtre affichée croît de 0 à
  24 h depuis le démarrage ; le registre vit en mémoire et repart de zéro au
  redémarrage du processus.
- « Recherches perso » : âge du plus ancien dernier scan réussi ; une recherche
  jamais scannée compte depuis sa première observation.
- « Requêtes Vinted (60 s) » : requêtes émises sur les 60 dernières secondes.

Un 403 est classé par les seuls en-têtes de la réponse — `cf-ray` ou
`server: cloudflare`, cookie `datadome`, sinon « indéterminé » — et l'indice apparaît
dans la raison du statut de pause. Cinq cycles consécutifs sans collecte du flux
Livres notifient une fois ; la reprise notifie une seconde fois.

## Journal des réglages d'alerte

Une table `alert_setting_changes` conserve l'historique des réglages modifiés : une
ligne par champ, avec l'ancienne valeur, la nouvelle, l'horodatage UTC et l'origine
du geste. Elle répond à une question que l'état courant ne peut pas : pourquoi une
alerte ne remonte plus, alors que son plafond ou ses exclusions ont changé hier.

| Colonne | Type | Rôle |
| --- | --- | --- |
| `id` | UUID | identifiant de la ligne |
| `alert_id` | UUID, FK `alerts.id` `ON DELETE CASCADE` | alerte concernée ; supprimer l'alerte supprime son journal |
| `field` | `VARCHAR(64)` | nom du champ modifié, tel que l'attribut du modèle |
| `old_value` | JSON/JSONB nullable | valeur avant le geste, `NULL` si le champ était vide |
| `new_value` | JSON/JSONB nullable | valeur après le geste, `NULL` si le champ a été vidé |
| `changed_at` | `TIMESTAMP WITH TIME ZONE` | instant UTC de l'écriture |
| `origin` | `VARCHAR(200)` | module et fonction qui ont posé la valeur |

Le seul index est celui de lecture : `(alert_id, changed_at)`, soit l'histoire
d'une alerte dans l'ordre. Le journal n'est ni un index de recherche, ni un
journal d'audit global.

Champs suivis : `max_item_price_cents`, `include_keywords`, `exclude_keywords`,
`variants`, `accepted_conditions`, `listing_baseline`. Rien d'autre — un statut, un
nom ou une cote de série ne sont pas des réglages de veille. Créer une alerte
n'écrit aucune ligne : une création n'a pas d'ancienne valeur, et la ligne d'alerte
la porte déjà. Il n'y a pas de reprise du passé : l'historique commence à la mise en
service, et aucune valeur antérieure n'est inventée.

L'écriture vit dans [un seul module](../../app/db/alert_settings_journal.py) et se
déclenche sur le `flush` de la session SQLAlchemy, pas dans les gestionnaires :
bot, tâche de fond et script de masse en héritent sans le savoir. `origin` est relevé
au moment de l'affectation, ce qui distingue une action du bot d'une modification de
masse, sans dépendre de la pile au moment du `commit`. Une affectation sans
changement n'a pas d'historique d'attribut et n'écrit donc rien ; un geste qui
touche trois réglages écrit trois lignes, chacune avec sa propre ancienne valeur.
Le mécanisme ne peut pas enregistrer ce qui n'a pas changé — c'est ce qui rend
l'absence de ligne significative.

Le journal ne décide rien : ni le matching, ni la notification, ni l'affichage ne le
lisent. Il ne participe à aucune purge applicative et une restauration de sauvegarde
le restaure avec le reste de la base, sans le reconstruire.

## Preuves existantes et limites

| Contrat | Contrôles à sélectionner |
| --- | --- |
| Dédoublonnage et état durable | [ingest API](../../tests/integration/test_ingest_api.py), [sérialisation](../../tests/integration/test_ingest_serialization.py) |
| Matching pur | [règles](../../tests/unit/test_matching_rules.py) |
| Envoi et enrichissement | [ingest→Telegram](../../tests/unit/test_ingest_to_telegram.py), [use case](../../tests/unit/test_ingest_use_case.py) |
| Frontières de temps et résultat | [latence](../../tests/integration/test_alert_latency.py), [mesures](../alert-latency.md) |
| Observabilité de la scrutation | [métriques du poller](../../tests/unit/test_vinted_poller.py), [statut](../../tests/unit/test_status_command.py), [classification et compteurs](../../tests/unit/test_vinted_scraper.py) |
| Journal des réglages, par chemin d'écriture | [journal des réglages](../../tests/integration/test_alert_settings_journal.py) |

Ces contrôles sont des preuves seulement lorsqu'exécutés sur un arbre et une
sélection identifiés. Les benchmarks ont des transports simulés. La source catalogue
observée historiquement n'expose pas de date de publication ; la durée publication
→ détection n'est donc pas mesurée. Aucun budget local ne garantit une réception réelle.

## Admission des nouveautés — ADR-050

Une nouvelle alerte Vinted initialise son premier catalogue réussi en silence,
avec un jalon et des identifiants exclus persistants propres à l'alerte, même si
les identifiants étaient connus globalement. Les reprises appliquent la même règle.
Une collecte échouée n'initialise rien. Les anciennes alertes restent inchangées ;
pause et reprise ne réinitialisent pas le jalon. Une publication explicite avant
création est rejetée. Sans publication, le premier scan est la frontière : des
nouveautés avant ce scan peuvent manquer, et l'âge réel d'une ancienne annonce
découverte ultérieurement reste inconnu. Aucune date n'est déduite des identifiants.


Les indisponibilités CAL ne suspendent pas le service d'annonces : les notifications
utilisent le cache local et les collectes conservent leurs reprises espacées.
