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
Création, édition et scheduler partagent ce chemin et le travail par livre.
Le panneau et les notifications lisent les données persistées sans collecte.
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

[market_price_service](../../app/services/market_price_service.py) choisit le minimum
historique Chasse aux livres prouvé : « Très bon état + », 365 jours, livraison
incluse. La valeur numérique du graphique, sa date, sa provenance et les paramètres
sont conservés dans `source_history`. Une projection annuelle locale et un ancien
`historical_low_1y_cents` sans provenance ne remplacent jamais ce minimum.
Sans ce minimum, une offre courante admissible peut servir de référence, avec son
propre libellé ; le champ du prix bas reste non renseigné. Les observations locales
restent disponibles pour l'audit interne. Les frais article, livraison et total sont
conservés séparément quand la source les fournit.

`lot_reference()` est commun au résumé et au panneau : composition identifiée,
couverture complète des tomes demandés, une seule édition applicable par tome et
une référence admissible par ouvrage. Sinon : détails et raisons, **aucun total de
lot ni écart global**, aucun repli sur la série plus large. Naviguer sur un tome ne
transforme pas le lot annoncé. L'ancien cumul partiel est remplacé par F08 (ADR-016).

Un lot seulement dénombré est composé par déduction uniquement si le catalogue
contient exactement ce nombre de tomes identifiables, sans éditions doubles.
La présentation dit « composition déduite ». Une intégrale cataloguée en un ouvrage
n'est pas automatiquement un lot à composer.

Le **total de série** affiché exige une couverture complète, une seule édition par
tome et des tomes identifiés. Les agrégats partiels persistés ne sont pas présentés
comme le prix de la série complète. Tous les livres liés restent visibles, y compris
les tomes sans prix et les éditions alternatives identifiées par ISBN. Une annonce
mentionnant un tome absent ne se rabat pas sur le seul autre tome disponible.
Une mutation réelle des liaisons invalide les totaux transactionnellement ; les
livres, caches et observations partagés restent conservés.

## Absences et preuves

À l'envoi, une ligne chiffrée ou une indication courte décrit l'état disponible.
Les erreurs, tentatives et diagnostics détaillés restent internes. La comparaison
explicite sa base livrée et l'estimation Vinted incluant livraison et protection.
Le message envoyé reste un instantané.
Le panneau partagé relit uniquement le cache et conserve le tome sélectionné.
Ni consultation, ni réception d'annonce, ni clic ne déclenche de collecte.
Le panneau est invalidé après fermeture ; il n'est pas réécrit automatiquement.
La création conserve son message temporaire et attend la préparation des prix dans
une enveloppe bornée de vingt secondes, puis affiche les valeurs obtenues. Les
marchands sont persistés dès cette collecte sans détailler la carte de création.

Contrôles existants : [offres](../../tests/unit/test_market_offer_rules.py),
[comparateur](../../tests/unit/test_market_comparator.py),
[séries](../../tests/unit/test_series_matching.py),
[validité de lot](../../tests/unit/test_lot_reference_validity.py),
[cycle du panneau](../../tests/unit/test_market_panel_lifecycle.py).
SQLite ne prouve pas les verrous PostgreSQL. Aucune lecture documentaire ne prouve
une collecte réelle ; les procédures de session et leurs permissions restent distinctes.

## Minimum source et fraîcheur indépendante

Le panneau affiche « Dernière offre connue », « Prix bas constaté » et « Dernières
offres connues ». La ligne du prix bas ne porte ni parenthèse technique, ni compteur
de relevés. Les marchands, états et liens sont associés au livre sélectionné.

Les offres courantes et l'historique ont des dates de réussite distinctes. Une
réussite partielle ne rafraîchit pas la composante en échec. Une réponse vide valide
peut dater la vérification sans effacer le dernier prix connu ; la date de ce prix
reste sa collecte réelle. « Dernière MAJ » distingue offres et historique si leurs
dates diffèrent. Ni tentative, ni date de changement du prix ne remplace une collecte
réussie. Aucun ancien cache ne reçoit une date inventée.

Chaque composante devient éligible sept jours après sa réussite. Le scheduler
existant réserve des créneaux persistés `next_refresh_at` aux passages de :15 et
:45, huit livres au plus, concurrence deux. Les reprises catalogue sont bornées à
quatre alertes par passe. Les réservations en retard excédant le lot courant sont
reportées dans de futurs créneaux. « Prochaine MAJ » lit cette réservation, sinon
annonce l'attente de programmation, sans date décorative. Après échec, les reprises
sont espacées de six heures, un jour puis sept jours ; elles conservent les valeurs
et dates de réussite. Les appels simultanés d'un même livre sont mutualisés dans
l'unique processus ; deux passes de prix ne s'exécutent pas simultanément.

La migration 0015 ajoute les champs de provenance et programmation sans créer de
réussite historique. La [reprise progressive](../operations/chasse-weekly-prices.md)
nécessite un inventaire puis une autorisation explicite avant écriture réelle.
La [procédure d’accès](../operations/chasse-access.md) décrit l'authentification,
les priorités de configuration et la maintenance intégrée sans cron supplémentaire.

Une panne technique de la recherche CAL par ISBN ne déclenche pas de recherche
par titre. Le repli bibliographique est réservé aux résultats vides valides ou
aux identités rejetées ; une restriction source arrête le parcours à l'étape où
elle apparaît. Les diagnostics par étape restent internes. La reprise du pilote
est manuelle, bornée et soumise à son échéance persistée ; sa procédure canonique
est la [livraison pilote](../operations/chasse-pilot-release.md).

## Diagnostic et bilans de rafraîchissement

Le bilan automatique est consolidé par occurrence de passe complète, y compris
si la sélection est vide ou interrompue. Les listes détaillées de fraîcheur
restent des diagnostics ; elles ne sont plus envoyées séparément dans ce flux.
Les compteurs distinguent traitement, offre actualisée, absence normale d'offre,
dégradation fournisseur, exception, caches réutilisés, tentatives non effectuées et livres non parcourus.
Un incident historique ne transforme pas une collecte courante réussie en panne.
Le diagnostic distingue les composantes partiellement actualisées sans attribuer
une panne globale à un fournisseur dont la collecte courante a répondu.

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
