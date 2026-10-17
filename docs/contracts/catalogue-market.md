# Contrat catalogue et marché BD

Statut : contrat actuel. Périmètre : identité, composition, comparabilité et validité
locale des références. Aucune formule n'est redéfinie ici.

## Identité avant cote

[BookSearchUseCase](../../app/use_cases/book_search.py) distingue `found`, `ambiguous`,
`partial`, `not_found`, `unavailable`. Les nouvelles créations Telegram utilisent
une projection partagée : seul `found` confirme l'ISBN ; un candidat partiel
n'alimente ni identité confirmée ni `BookContext`. Les œuvres homonymes se choisissent
avant les éditions. Choix et confirmation sont revalidés ; un clic ne prouve rien seul.

La BNF est primaire ; OpenLibrary puis Google Books configuré sont des replis
non confirmants, de provenance déclarée, jamais un remplacement d'une notice BNF
contradictoire. Un auteur explicitement demandé peut justifier le repli si tous
les homonymes BNF ont un auteur connu différent (ADR-045). L'IA ne confirme aucun ISBN.
Une série se détecte à partir des notices réellement rendues, pas d'une liste d'œuvres.
La composition par tomes est bornée à 12, BNF complétée par OpenLibrary sans substitution.

Le [verdict catalogue](../../app/domain/rules/catalogue_search.py) conclut à l'absence
de catalogue seulement si une autorité au moins a été consultée, aucune n'a échoué
et aucune notice n'a été trouvée. Une panne n'est pas une œuvre non cataloguée.

Une création BD persiste sa pause avant les attentes, termine le FSM puis prépare
catalogue et cotes ; les prix sont relus dans sa session avant activation. Une
préparation finie ne garantit pas un prix. Intention durable, version et reprise :
[préparation BD](../bd-preparation.md). L'édition existante peut conserver un ISBN
volontairement saisi non confirmé ; ce n'est pas la règle des nouvelles créations.
Le [retrait ISBN](../isbn-removal.md) conserve les critères manuels et invalide les
résultats tardifs. Il précise les amendements F06 à ADR-044.

## Une collecte et une politique de comparabilité

Toute écriture passe par [book_market_pricing](../../app/services/book_market_pricing.py) :
`price_book_now` pour un écran qui attend, `schedule_book_pricing` pour le fond.
Création, édition, panneau et scheduler partagent ce chemin et le travail par livre.
Aucun réseau sous verrou d'alerte ; relire liaisons et caches après acquisition du
verrou avant de persister un total. Une tentative et un relevé sont des faits distincts.

[market_offer](../../app/domain/rules/market_offer.py) est l'unique filtre : occasion,
état déclaré au moins « Très bon », marchand non exclu. Un état non qualifié
(« Occasion », `None`) reste acceptable : absence de détail ne signifie pas mauvais état. Ne pas réinventer ce filtre par
provider ou écran. Une offre sans port connu ne devient pas un total livré égal au
prix article. L'ISBN invalide n'est pas envoyé aux fournisseurs.

Pour Chasse-aux-livres : conserver `title_similarity` et `TITLE_COHERENCE_MIN`, sans
dictionnaire d'œuvres figé. L'ISBN affiché exactement égal à l'ISBN attendu prouve
l'identité et prime sur le veto de titre (`isbn_proven`, ADR-035). Sans cette preuve,
le veto demeure ; ne pas abaisser le seuil pour compenser.

## Références et lots

[market_price_service](../../app/services/market_price_service.py) choisit une
projection annuelle documentée admissible, sinon une offre courante valide.
La projection annuelle porte dates de calcul, expiration, première/dernière observation,
date et snapshot du minimum, fournisseur et nombre d'observations. La lecture
revalide la fenêtre de 365 jours ; un ancien `historical_low_1y_cents` nu ne suffit pas.
Les [tests annuels](../../tests/integration/test_stale_certified_lows.py) et
[tests de lot](../../tests/unit/test_lot_reference_validity.py) bornent les preuves.
Le libellé « prix bas constaté » décrit nos relevés, pas un historique exhaustif du marché.

`lot_reference()` est commun au résumé et au panneau : composition identifiée,
couverture complète des tomes demandés, une seule édition applicable par tome et
une référence admissible par ouvrage. Sinon : détails et raisons, **aucun total de
lot ni écart global**, aucun repli sur la série plus large. Naviguer sur un tome ne
transforme pas le lot annoncé. L'ancien cumul partiel est remplacé par F08 (ADR-016).

Un lot seulement dénombré est composé par déduction uniquement si le catalogue
contient exactement ce nombre de tomes identifiables, sans éditions doubles.
La présentation dit « composition déduite ». Une intégrale cataloguée en un ouvrage
n'est pas automatiquement un lot à composer.

Le **total de série** persistant peut être partiel, avec couverture annoncée ; il
n'est jamais le prix du lot vendu. Une mutation réelle des liaisons invalide ce
total transactionnellement ; l'ajout idempotent ne le fait pas. La lecture masque
un total incompatible sans calculer une somme de remplacement. Les livres/caches
partagés restent conservés. Le panneau affiche aussi l'état des tomes sans prix.

## Absences et preuves

À l'envoi, une ligne non chiffrée décrit l'état réel : sans relevé, échec de collecte,
aucune offre datée, composition inconnue ou catalogue épuisé. Un message envoyé
reste un instantané.
Le panneau partagé relit le cache et conserve le tome sélectionné. Il ne relance
pas de collecte lorsqu'une cote exploitable ou une collecte réussie sans offre
existe ; sans première cote, il déclenche la cotation initiale par le mécanisme
existant, avec la garde de collecte en vol et le repos des échecs. Une résolution
catalogue manquante peut être demandée une fois par période de 24 h ; la première
cotation du tome retrouvé est ensuite demandée, sans repli sur un autre tome. Le
message ouvert n'est pas actualisé automatiquement et le panneau est invalidé après
fermeture (ADR-042).

Contrôles existants : [offres](../../tests/unit/test_market_offer_rules.py),
[comparateur](../../tests/unit/test_market_comparator.py),
[séries](../../tests/unit/test_series_matching.py),
[validité de lot](../../tests/unit/test_lot_reference_validity.py),
[cycle du panneau](../../tests/unit/test_market_panel_lifecycle.py).
SQLite ne prouve pas les verrous PostgreSQL. Aucune lecture documentaire ne prouve
une collecte réelle ; les procédures de session et leurs permissions restent distinctes.

## Minimum unique et fraîcheur — ADR-050

Le panneau affiche un seul « Prix bas constaté », sans date ni période de relevés.
La fenêtre de calcul de 365 jours ne prouve pas une année complète de couverture du
marché. Les données des anciens agrégats restent conservées en interne, sans ligne
« Ancien minimum (informatif) » ni compteur de relevés affiché. Un point historique
fournisseur exige montant livré, date et provenance ; il est persisté séparément
des offres disponibles et intégré aux observations admissibles des 365 derniers jours.
Une collecte récente ne devient jamais la date d'un prix historique. Les totaux de
série utilisent la même projection centrale et les règles d'occasion/état/marchand.

La tentative, la réussite des offres et la récupération des preuves historiques
ont des dates distinctes. Le pied du panneau porte la date de la **dernière
vérification réussie** du livre et du tome affichés : elle avance après une collecte
réussie même si le prix ne change pas, et reste inchangée après un échec. Une
vérification qui n'a jamais réussi s'affiche « jamais » ; un cache historique sans
date de succès exploitable s'affiche « inconnue ». La date d'un changement de prix
ou d'une tentative échouée ne la remplace jamais. La ligne « Prochaine » n'apparaît
que si le planificateur confirme qu'une passe complète prend ce livre en charge.

Un minimum ancien dans sa fenêtre reste valide même si aucun nouveau relevé ne l'égale.
Une collecte vide réussie vaut résultat ; une panne sans résultat exploitable
conserve les dernières offres connues sans renouveler leur fraîcheur. Le panneau ne
présente pas cette conservation comme une actualisation ni comme une panne : l'échec
technique reste dans les logs. Le renouvellement Chasse valide/injecte un jeton et
ne collecte pas toutes les alertes.

## Diagnostic et bilans de rafraîchissement

Le bilan automatique est consolidé par occurrence de passe complète, y compris
si la sélection est vide ou interrompue. Les listes détaillées de fraîcheur
restent des diagnostics ; elles ne sont plus envoyées séparément dans ce flux.
Les compteurs distinguent traitement, offre actualisée, absence normale d'offre,
dégradation fournisseur, exception, caches réutilisés, tentatives non effectuées et livres non parcourus.
Un incident historique ne transforme pas une collecte courante réussie en panne.
La carte indique des données partiellement actualisées sans attribuer une panne
globale à un fournisseur dont la collecte courante a répondu.

Quand toutes les sources sont ignorées, le cache porte `skipped` : aucune
collecte courante ni absence d’offre n’est attestée. Les prix, offres et dates
antérieurs sont conservés, y compris lorsqu’ils ne sont plus comparables.
Cet état ne permet pas le raccourci de réutilisation d’un cache valide à la création.

Les tentatives fournisseur portent opération, catégorie contrôlée, statut HTTP et
durée. Une réponse invalide ne vaut pas recherche vide. Le silence des snapshots
ne suffit pas à établir une panne. Un timeout global porte un diagnostic de
collecte, sans désigner tous les sites comme défaillants.

Les passages incrémentaux signalent les nouveaux incidents, les aggravations et
les rétablissements observés. L'absence de sollicitation ne rétablit pas une source.
La dernière observation est distincte de l'état effectivement notifié : une
amélioration partielle silencieuse actualise la référence ; le retour à une panne
totale peut donc produire une aggravation. Les changements non remis restent en
attente de notification. Observations, état notifié et changements en attente sont
persistés dans la même clé Redis ; l'ancien format d'incidents reste lisible.
Si l'incident n'a jamais pu être remis et qu'un rétablissement est réellement
observé, une synthèse des deux événements reste à remettre. Elle indique que
l'incident est passé ; elle ne présente pas cette panne comme actuelle. Un échec
de remise conserve cette synthèse, y compris si les passes suivantes n'interrogent
pas la source. Une nouvelle panne avant remise conserve la synthèse précédente et
signale séparément le nouvel incident en cours. La remise confirmée retire les
changements en attente ; elle ne garantit pas une livraison exactement une fois.
L'état utilise Redis avec repli local dans l'unique processus ; le repli ne survit
pas au redémarrage. Le dédoublonnage de bilan conserve sa fenêtre d'une heure.
Un échec ou une annulation d'envoi libère le créneau ; une remise Telegram ambiguë
ou partielle ne permet pas de garantir exactement un message par destinataire.
