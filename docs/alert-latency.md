# F05 — frontières de latence

Statut : contrat de mesure actuel et résultats historiques attribués ci-dessous. Périmètre : frontières de latence locale ; aucune garantie de réception réelle.

L'invariant Telegram d'abord est un ordre, pas une garantie de livraison sous un
seuil. Aucun budget opérationnel, timeout ou politique de tentative n'est ajouté.
Un accusé exploitable atteste un reçu API, pas la lecture humaine. Les benchmarks
avec doubles ne sont pas des mesures de production.

## Événements

`alert_latency` utilise `time.monotonic`, des `duration_ms` et `duration_unit=ms`.
Les timestamps monotoniques absolus ne sont pas enregistrés, à deux exceptions près :
`cycle_started_monotonic`, début de cycle du poller, et `discovered_monotonic`, retour
de lecture d'un catalogue ; leurs valeurs ne se lisent que relativement à l'horloge de
l'instance qui les émet. Les événements
portent les instants capturés, pas leur date d’émission : le diagnostic pré-appel
est émis à la fin de la tentative pour ne pas entrer dans son chrono. `null` signifie une
frontière inconnue/une horloge défaillante, jamais zéro. Pas de SQL, réseau ou
stockage supplémentaire pour instrumenter ; les champs sont des logs existants
ou de la même sortie structurée. Les échecs ordinaires du diagnostic sont ignorés
sans log récursif et ne remplacent pas les résultats ou exceptions métier.

| phase | Début → fin |
| --- | --- |
| collection | Avant fetch_vinted_page → retour de la liste parsée, ou exception/annulation ; réseau de collecte et parsing inclus, aucune date de publication supposée |
| catalogue_read | Aucune durée : une ligne par lecture de catalogue, émise en `finally` à la fin de `poll_vinted_once`, donc après l'ingestion et l'envoi Telegram du lot, y compris sur une sortie anticipée sans nouveauté ; son horodatage ne date pas la lecture (`collection`, même `collection_id`, porte cet instant, redonné par `discovered_monotonic`) ; une lecture échouée garde `result="error"`/`"cancelled"` et aucun compteur |
| discovery_to_entry | Retour de collecte local → entrée dans ProcessListingUseCase.execute ; seulement lorsque connu, conversion et queue poller incluses |
| semaphore_wait | Avant acquisition du sémaphore poller → acquisition/abandon ; conversion du payload exclue |
| guard_wait | Avant acquisition de la garde F03-A → acquisition/abandon ; traitement/rollback exclus |
| pre_first_telegram_call | Garde acquise → début de la première attente du client Telegram ; lectures/commits initiaux, matching, cache et construction du message inclus |
| recipient_wait | Matches initiaux commités → début de traitement de ce destinataire ; inclut les transports et persistances des précédents, pas la préparation commune |
| transport_attempt | Avant await du client → résultat validé ou exception/annulation, une ligne par photo/texte et numéro d'essai ; horodaté avant les logs de résultat |
| retry_pause | Avant attente de retry photo → fin ou interruption ; requested_delay_ms garde la valeur demandée existante, pas une durée garantie |
| entry_to_ack | Entrée du cas d'usage → réception validée du Message ; par destinataire, uniquement pour un accusé exploitable avec entrée connue |
| delivery_terminal | Entrée du helper Telegram → retour/exception/annulation ; transport, retry et programmation IA inclus, pas achèvement de l'IA |
| ack_to_persistence | Accusé validé → sortie du helper de marquage ; rollback, relecture et nouvel essai SQL F03-B inclus ; notified signifie commit terminé ou état durable relu, unknown/cancelled reste distinct |
| post_send_recalculation | Recalcul local déjà existant après tous les envois → retour/exception ; distinct du commit final |
| final_commit | Avant await du commit final → retour/exception ; le recalcul précédent est exclu |
| ingestion_terminal | Entrée du cas d'usage → sortie après le rollback protégé éventuel ; attempted, duplicate_no_send, no_match, no_eligible_recipient, error/cancelled distingués |
| background_start | Construction de la coroutine différée → première exécution de son wrapper |
| background_run | Début du wrapper → retour/exception/annulation ; scheduled_to_end_ms donne aussi construction → fin |

Les horloges d'admission ne mesurent pas l'arrivée HTTP, l'authentification et la
validation avant la route. `final_commit` n'existe que si le commit a commencé.
Les annulations de marquage peuvent encore nécessiter le rollback extérieur :
`ingestion_terminal` en comprend l'attente. Aucun délai F03-A/F09 n'est étendu.

`ingestion_id` corrèle les phases d'une invocation ; `collection_id` corrèle le
lot collecté. Dès qu'ils sont connus, les mêmes listing_id/alert_id/attempt_id
que F03-B sont utilisés ; le reçu et son message_id restent uniquement en mémoire
et dans le diagnostic. La cotation partagée utilise book_id et peut ne pas avoir
un destinataire unique. Aucun titre, corps, payload, URL, secret ou texte libre
d'exception n'est ajouté. Les IDs servent aux logs, pas à des labels de métriques
à cardinalité illimitée.

## Lecture de catalogue

`catalogue_read` est la seule phase qui décrit un **catalogue lu**, et non une annonce
ingérée : une ligne par appel de `poll_vinted_once`. Elle porte `cycle` (index du cycle
déjà reçu en paramètre), `category` (le `category_slug` du catalogue), `target_hash`
(huit premiers caractères hexadécimaux du SHA-256 de l'URL cible : deux catalogues se
distinguent sans écrire l'URL ni le texte de recherche en clair), `items_received`
(annonces ramenées par `fetch_vinted_page`), `items_new` (annonces absentes de
`seen_ids`), `frontier_id` et `previous_frontier_id` du lot, et `discovered_monotonic`,
l'instant monotone du retour de cette lecture. Rien n'est collecté pour la produire :
aucun appel supplémentaire, aucun changement de cadence. La ligne ne porte pas de durée
(`duration_ms` reste `null`) : elle décrit ce que la lecture a ramené, pas combien de
temps elle a pris.

Où lire l'instant de la lecture. La ligne est émise en `finally`, à la fin du passage :
après l'ingestion du lot et après l'envoi Telegram, ou après la sortie anticipée qui suit
une lecture sans nouveauté. Son horodatage est donc décalé de tout le traitement du lot
et ne date pas la lecture. L'instant de la lecture est celui de la ligne `collection` du
même `collection_id`, émise au retour de `fetch_vinted_page` ; `catalogue_read` le
redonne avec `discovered_monotonic`, sur l'horloge monotone. Un intervalle de lecture se
calcule donc entre deux lignes `collection`, jointes à leur `catalogue_read` par
`collection_id` — le même identifiant qui rattache l'empreinte et les compteurs à leur
lecture —, ou entre deux `discovered_monotonic` de la même instance.

Une lecture qui n'a pas eu lieu ne fabrique pas de zéros. Si `fetch_vinted_page` lève ou
est annulé, la ligne part quand même, avec le marqueur de résultat déjà employé par
`collection` (`result="error"` ou `"cancelled"`, le type d'exception restant sur la
ligne `collection`) et `items_received`, `items_new`, `frontier_id`, `previous_frontier_id`
et `discovered_monotonic` à `null` : ils sont inconnus, pas vides. Sans frontière
précédente connue, `previous_frontier_id` vaut `null`, jamais `0`.

Ce que l'événement permet de calculer : le **débit par intervalle, par catalogue**.
Deux `catalogue_read` successives du même `target_hash` donnent, par leurs
`discovered_monotonic` — ou par les horodatages des lignes `collection` jointes par
`collection_id` —, l'intervalle de lecture de ce catalogue, et les `items_new` de la
seconde ce que cet intervalle a apporté de neuf ; sur plusieurs lectures, la somme des
`items_new` rapportée à l'écart entre la première et la dernière donne le débit de cette
période. C'est ce que `poller_cycle` ne peut pas faire : sa ligne additionne le flux BD
et la recherche personnalisée du même cycle et n'identifie que le cycle.

Ce qu'il ne permet pas : il borne l'instant de la lecture, il ne date pas l'apparition de
l'annonce chez Vinted, faute de source (voir plus bas) — il majore la latence
publication → détection, il ne la mesure pas. Et l'écart entre deux lectures n'est un
intervalle de scrutation que pour le flux BD : pour une recherche personnalisée, c'est
son propre rythme de passage (n cycles), jamais celui d'un cycle.

## Frontière du lot

`ingestion_terminal` porte quatre champs de position :
`external_id` de l'annonce, `frontier_id` (plus grand `external_id` numérique du lot
ramené par `fetch_vinted_page` ce cycle, calculé une fois par appel sur la liste déjà
en mémoire), `previous_frontier_id` (frontière du lot du passage précédent de la
même URL cible) et `cycle_started_monotonic` (début du cycle portant le lot).
`catalogue_read` reprend `frontier_id` et `previous_frontier_id` pour le lot entier,
par lecture et non par annonce.
Aucune requête ni cadence supplémentaire : le filtrage des
nouveautés reste une appartenance à `seen_ids`. Sans frontière précédente connue —
redémarrage, ou première interrogation de cette URL — le champ vaut `null`, jamais
`0`. L'accusé `entry_to_ack` ne change pas ; ces champs partent plus tard, avec la
phase terminale de l'ingestion.

Une frontière par `target_url` : le flux BD et chaque recherche personnalisée sont
des catalogues distincts, aux échelles d'identifiants non comparables. Un cycle
interroge le flux BD puis, s'il y en a, une recherche personnalisée ; les deux
écrivent leur propre entrée, et `previous_frontier_id` d'une URL ne provient jamais
du lot d'une autre. `cycle_started_monotonic` reste commun au cycle : c'est la même
horloge de début. Une recherche désactivée perd son entrée, donc une URL qui
disparaît puis revient repart de `null` — aucune frontière fantôme héritée.

Trois limites de lecture. D'abord, ces champs ne sont émis que pour les annonces
réellement ingérées : une annonce vue puis filtrée (déjà en `seen_ids`) n'émet
aucune ligne terminale, et une annonce sans destinataire émet une ligne mais aucune
alerte. Ensuite, le débit d'annonces par intervalle ne se déduit pas du nombre de
lignes d'ingestion : il se lit sur les lignes `catalogue_read`, une par lecture de
catalogue, avec l'instant de lecture qui va avec — `discovered_monotonic`, ou
l'horodatage de la ligne `collection` du même `collection_id`, jamais l'horodatage de
`catalogue_read`, émis après l'ingestion et l'envoi — et jamais avec l'heure d'ingestion,
qui dépend de la file d'ingestion et non de la scrutation ; `cycle_started_monotonic`
sert seulement à regrouper les lignes d'ingestion d'un même cycle, et la ligne
`poller_cycle` additionne les deux catalogues d'un cycle sans les distinguer. Enfin,
la frontière ne date pas l'annonce : elle borne le lot, une annonce absente du lot
ne laissant aucune trace (ni avant, ni après).

Une dernière limite change la lecture de la mesure. Le flux BD est interrogé à
**chaque** itération par le premier `poll_vinted_once` du cycle, la recherche
personnalisée **une fois sur n**, en alternance (`custom_searches[poll_cycle %
len(custom_searches)]`). Pour une recherche donnée, `previous_frontier_id` ne
remonte donc pas au cycle précédent mais à son dernier passage, plusieurs cycles en
arrière. Un débit par intervalle reste calculable par catalogue — c'est ce que
`catalogue_read` apporte — à condition de rapporter `items_new` à l'écart entre deux
instants de lecture **de ce catalogue** : un cycle pour le flux BD, n cycles pour une
recherche personnalisée. Ne pas calculer de débit de recherche personnalisée en croyant
lire un intervalle d'un cycle.

## Publication → détection : source absente

`listings.published_at` existe (colonne nullable, migration initiale) et le champ d'ingestion
aussi, mais aucun producteur ne les remplit sur le chemin du poller. Une sonde unique et
encadrée (`scripts/probe_vinted_published_at.py`, exécutée le 2026-10-13) a listé les clés d'un
item réellement renvoyé par `/svc-catalogue/items` : `id`, `title`, `price`, `item_box`,
`photo`, `photos`, `url`, `user`, `view_count`, `total_item_price`, `service_fee`,
`search_tracking_params`, `promoted`, `show_1st_time_seller_discount`, entre autres.
**Aucun horodatage de publication n'y figure** — et le seul nom retenu par une détection de
clés de date, `show_1st_time_seller_discount`, est un booléen : le « time » de « first time ».

Conséquence : la latence publication → détection reste **non mesurable**, faute de source. La
colonne est conservée pour un appelant d'API externe qui fournirait la date ; elle reste NULL
sur le chemin du scan, et la mesure publication → détection n'est pas ajoutée à `/status`.
Couper ou réduire la couverture nocturne (7 h/j de suspension) ne peut donc pas se trancher
sur des données de publication : il faudrait une autre source.

## Résultats et fond

Refus établis, inconnus, réponses inutilisables et annulations conservent une durée
terminale. Ils n'obtiennent pas de faux temps jusqu'à accusé : entry_to_ack n'est
pas émis ; entry_to_ack_ms du terminal est null. Une tentative photo inconnue ne
s'efface pas lorsque un repli est refusé (F03-B). Un accusé suivi d'une panne SQL
reste un accusé ; sa persistance est décrite séparément. Aucune proportion n'est
calculée : toute analyse future doit établir son dénominateur et son effectif.

Le nombre `attempts` d'une ingestion compte les appels éligibles au helper d'envoi,
y compris un rejet local avant réseau ; ce n'est pas le nombre de requêtes Telegram.
Les annonces écartées par les vus du poller ne sont pas des appels d'ingestion.

Le suivi du fond porte seulement sur les travaux issus de la chaîne d'alerte :
liaison bibliographique, série, pipeline IA, cotation programmée et compteur de cote
envoyée. Les appels hors de ce contexte ne sont pas instrumentés. Le contexte est
copié lors de la programmation : une tâche ne reprend pas le destinataire suivant.
Le wrapper est la même tâche safe_create_task, sans task enfant ni autre registre ;
F09, ses cleanups et sa propagation des annulations sont inchangés. Fermer une
coroutine jamais démarrée ne crée pas de coroutine métier orpheline.

Le fond peut commencer avant le retour du cas d'usage, pendant un await de la
transaction finale. `returned` indique un retour de coroutine, pas un fournisseur
ayant répondu ou une cote utilisable ; les erreurs absorbées restent décrites par
les diagnostics métier existants. La coalescence de cotation est inchangée : un
travail partagé n'est pas artificiellement dupliqué pour chaque demande.

## Status et benchmarks

`/status` affiche « Réception → marquage (notifiés) » : notified_at - received_at,
500 matches maximum, réception dans les sept derniers jours, tri par réception
(pas par notification), écarts négatifs exclus. Pending et tentatives sans accusé
ne sont pas représentés par ce dénominateur. F01 conserve la réception initiale ;
le marquage est horodaté avant UPDATE/commit et une reprise SQL peut le déplacer.
Ce n'est ni un chrono d'accusé ni la fin du commit ; la requête est inchangée.

Les six benchmarks gardent tous leurs seuils :
- Execute complet : payload/bot construits avant le départ du chrono, retour après
  marquage et commit final ; Telegram doublé, IA coupée, SQLite. 81 alertes semées,
  41 BD évaluées par la requête filtrée de ce scénario ; p95/p99 < 500 ms.
- Doublon : entrée execute → retour sans envoi ; p95 < 500 ms.
- CPU : passes de matching sur 81 alertes, hors SQL/Telegram ; médiane des moyennes
  par évaluation < 100 µs, pas un percentile d'appels individuels.
- Alerte de série : entrée execute → retour ; les coroutines sont retenues puis
  exécutées par le harness. L'ordre BNF après retour est propre au test, pas une
  garantie de production ; p95 < 500 ms. Le délai BNF de 300 ms est simulé.
- Sélection de notices doublées : entrée lookup_series_notices → retour ; p95 < 5 ms.
- Liaison de six tomes : entrée link_books_from_bnf → retour ; écriture et
  programmation de cotation incluses, achèvement des collectes exclu ; p95 < 100 ms.

L'overhead doit être comparé à la base dans les mêmes conditions locales, sans
concurrence de tests. Il n'est pas supposé nul. Les logs sont best effort : absence
d'événement après crash/arrêt forcé ne signifie ni durée zéro ni succès. Aucune
mesure de production ni nouvelle garantie de livraison/durabilité.

## Comparaison locale de l'overhead

Base 446766f et F05 : même Python/venv, SQLite en mémoire, fixtures et doubles,
IA coupée, trois exécutions séparées par version, sans autre test lancé par cette
procédure. Les six seuils restent inchangés et tous les passages ont réussi.
Valeurs ci-dessous : médiane des trois p95 en ms ; pour le CPU, médiane des trois
médianes de moyenne par évaluation en µs.

| Scénario | Base, profil ordinaire | F05, profil ordinaire | Base, INFO JSON | F05, INFO JSON |
| --- | ---: | ---: | ---: | ---: |
| Execute complet | 8,29 | 8,94 | 7,63 | 9,12 |
| Doublon | 5,06 | 5,78 | 5,02 | 5,51 |
| CPU, µs/évaluation | 23,7 | 23,9 | 23,7 | 23,7 |
| Execute de série | 3,59 | 4,11 | 3,81 | 4,87 |
| Sélection de série | 1,23 | 1,81 | 1,22 | 1,22 |
| Liaison de série | 17,22 | 18,61 | 17,22 | 17,30 |

Le profil JSON active setup_logging(INFO) et le même JSONFormatter pour les deux
versions, avec la capture pytest et une sortie locale : ce n'est pas le coût de
Docker/journalisation en production. Les écarts observés ne sont pas un coût
causal isolé : les scénarios non instrumentés varient également. À titre de
variabilité, les p95 Execute ordinaires sont 10,36/8,29/7,30 ms à la base et
7,46/8,94/12,37 ms en F05 ; avec INFO JSON, 7,86/7,61/7,63 et 9,12/9,22/8,44 ms.
Aucune promesse de coût nul ou de livraison réelle n'en découle.
