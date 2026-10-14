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
5. Après les envois, lancer via `safe_create_task` la liaison bibliographique et la
   cotation manquante dans leurs sessions propres. L'IA est lancée depuis
   `telegram_service.py` avec le vrai identifiant du message accepté.

Le titre et l'ISBN validé partent ensemble à tous les fournisseurs dans la cotation
centrale, repli par titre compris. Cette collecte sert les annonces suivantes.
La préparation de checkout n'existe qu'au clic ultérieur ; aucune préparation à l'envoi.

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
Préserver `_get_send_listing_alert()` : les tests d'ingestion patchent ce réexport.

## Preuves existantes et limites

| Contrat | Contrôles à sélectionner |
| --- | --- |
| Dédoublonnage et état durable | [ingest API](../../tests/integration/test_ingest_api.py), [sérialisation](../../tests/integration/test_ingest_serialization.py) |
| Matching pur | [règles](../../tests/unit/test_matching_rules.py) |
| Envoi et enrichissement | [ingest→Telegram](../../tests/unit/test_ingest_to_telegram.py), [use case](../../tests/unit/test_ingest_use_case.py) |
| Frontières de temps et résultat | [latence](../../tests/integration/test_alert_latency.py), [mesures](../alert-latency.md) |

Ces contrôles sont des preuves seulement lorsqu'exécutés sur un arbre et une
sélection identifiés. Les benchmarks ont des transports simulés. La source catalogue
observée historiquement n'expose pas de date de publication ; la durée publication
→ détection n'est donc pas mesurée. Aucun budget local ne garantit une réception réelle.
