# Contrat catalogue et marché BD

Statut : contrat actuel. Périmètre : identité, composition, comparabilité et validité
locale des références. Aucune formule n'est redéfinie ici.

## Identité avant cote

[BookSearchUseCase](../../app/use_cases/book_search.py) distingue `found`, `ambiguous`,
`partial`, `not_found`, `unavailable`. Les nouvelles créations Telegram utilisent
une projection partagée : seul `found` confirme l'ISBN ; un candidat partiel
n'alimente ni identité confirmée ni `BookContext`. Les œuvres homonymes se choisissent
avant les éditions, avec leurs auteurs et leurs ISBN distincts. Choix et confirmation
sont revalidés ; un clic ne prouve rien seul.

La liste et les boutons de tomes partagent une projection de présentation, limitée
à la série retenue et dédupliquée par numéro. L'édition confirmée dans la préparation
reste prioritaire ; plusieurs éditions sans choix établi restent ambiguës. Cette
projection ne modifie ni les livres liés, ni les compositions ou totaux du marché.
Les offres d'une édition ne sont pas affichées sous l'identité d'une autre.

La BNF est primaire ; OpenLibrary puis Google Books configuré sont des replis
non confirmants, de provenance déclarée, jamais un remplacement d'une notice BNF
contradictoire. Un auteur explicitement demandé peut justifier le repli si tous
les homonymes BNF ont un auteur connu différent (ADR-045). L'IA ne confirme aucun ISBN.

Dernier recours, et pour l'ajout seulement : les [Nouveautés Éditeurs de la BnF](../../app/providers/nouveautes_editeurs.py),
site public des annonces de parution. Il est interrogé **uniquement** quand la BNF
n'a produit aucune notice confirmante et qu'OpenLibrary puis Google Books se sont
tus — et pas du tout quand la BNF confirme (ADR-055). Une déclaration d'éditeur y est
une identité *déclarée* : sa provenance et son état de dépôt légal (« Reçu par la BnF »
ou « Annoncé éditeur ») accompagnent la réponse, et un ISBN déclaré n'est jamais
présenté comme confirmé par le catalogue. La BNF reste la seule autorité d'identité ;
une déclaration n'est admise que sur un ISBN-13 égal au code demandé, une date de
parution passée, un titre au seuil de similarité du service, et des auteur et éditeur
cohérents avec ceux fournis. Une parution future est refusée comme annonce
(« parution annoncée, pas encore parue »), et tout doute résiduel refuse.
La veille marché n'ouvre pas cette source : elle garde son budget HTTP.
Une série se détecte à partir des notices réellement rendues, pas d'une liste d'œuvres.
La composition par tomes est bornée à 12, BNF complétée par OpenLibrary sans substitution.

Le [verdict catalogue](../../app/domain/rules/catalogue_search.py) conclut à l'absence
de catalogue seulement si une autorité au moins a été consultée, aucune n'a échoué
et aucune notice n'a été trouvée. Des résultats écartés faute de preuve d’identité
ne prouvent pas cette absence (`identity_unresolved`). Une panne n'est pas une œuvre non cataloguée.

Une création BD persiste sa pause avant les attentes, termine le FSM puis prépare
catalogue et cotes ; les prix sont relus dans sa session avant activation. Une
préparation finie ne garantit pas un prix. Intention durable, version et reprise :
[préparation BD](../bd-preparation.md). L'édition existante peut conserver un ISBN
volontairement saisi non confirmé ; ce n'est pas la règle des nouvelles créations.
Le [retrait ISBN](../isbn-removal.md) conserve les critères manuels et invalide les
résultats tardifs. Il précise les amendements F06 à ADR-044.

La découverte automatique exige un titre d’œuvre cohérent, un auteur attendu
explicitement établi et une langue française prouvée. Un prénom partagé ne suffit
pas ; une langue absente ou indéterminée entraîne une abstention. Une autre langue,
un auteur contradictoire ou un autre tome entraînent un rejet. Les candidats de
création peuvent rester présentés sans ISBN confirmé ; une confirmation exige la
preuve BNF correspondante. Les métadonnées d’une œuvre OpenLibrary ne prouvent pas
la langue de son édition : celle-ci doit être lue sur l’édition.

Un ISBN identifie une édition, sans établir seul l’intention d’une alerte ni la
composition d’une annonce. Un code vendeur valide contradictoire ne se rabat pas
sur une autre édition en cache. Pour une demande individuelle, une composition
explicite ne prouve pas ce tome seul. Une sélection de plusieurs tomes peut notifier
n’importe lequel des tomes ciblés, y compris dans un lot ; le seul nom « tomes 1 et 2 »
n’impose pas leur présence simultanée. La couverture complète est requise uniquement
pour une intention explicite enregistrée dans le nom ou les inclusions : « lot complet »,
« coffret complet », « collection complète », « série complète » ou « intégrale ».
Un mot « lot » isolé ne suffit pas à imposer cette intention. Cette pertinence de
notification ne prouve ni l’identité d’un livre seul ni la référence de prix du lot.
Les alertes sans livre restent envoyables ; la confirmation de livre,
après les envois, s’abstient si les preuves d’identité ou de langue manquent. Un ISBN
explicitement attendu par l’alerte et confirmé concordant dispense de l’égalité
des titres et de la présence d’un auteur ; les contrôles de langue française,
de tome et de composition restent applicables. Aucun appel
catalogue n’est ajouté avant Telegram. Mesures et limites :
[bilan d’identité](../evaluations/matching-discovery-2026-10-23.md).

## Une collecte et une politique de comparabilité

Toute écriture passe par [book_market_pricing](../../app/services/book_market_pricing.py) :
`price_book_now` pour un écran qui attend, `schedule_book_pricing` pour le fond.
Création, édition et scheduler partagent ce chemin et le travail par livre.
Le panneau et les notifications lisent les données persistées sans collecte.
Aucun réseau sous verrou d'alerte ; relire liaisons et caches après acquisition du
verrou avant de persister un total. Une tentative et un relevé sont des faits distincts.
Un prix ne date une réussite courante que si la source qui le fournit porte une
tentative `current` `success` ; un prix sans cette preuve reste consultatif et
n'avance pas la fraîcheur. Une collecte courante utilisable ou un prix confirmé
remplace les offres courantes ; une offre antérieure n'est réinjectée que si
la collecte courante n'est pas utilisable et ne porte aucun prix confirmé. Elle
reste une observation datée, jamais une nouvelle preuve courante. Une passe
limitée à l'historique ne réécrit ni prix, ni offres, ni statut, ni date de succès
courants ; elle ne met à jour que la composante historique et son échéance.

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

## Admission d'un tome et cote de création

Un tome de série n'est retenu qu'avec une date de parution **prouvée et déjà passée**.
La preuve ne se lit pas n'importe où dans la zone fixe `100$a` : ses positions 0-7
portent la **date d'entrée au catalogue**, c'est-à-dire le jour où la BNF a créé la
notice, et les positions 9-12 la **Date 1** de parution, gouvernée par le type de
date en position 8. Un livre catalogué hier n'est pas un livre paru hier : la date
d'entrée n'est jamais lue comme une date de parution. Seul le type `d` (« publié en
une seule année civile ») est admis ; `u` (inconnu), les types de ressources
continues et les types d'édition non datés ne prouvent rien. Un jour n'est affiché
que s'il vient d'un champ de publication (`214$d`, par exemple `DL 06/02/2026`) ;
`DL 2026` et une année nue ne prouvent que l'année, et une année déclarée sur une
notice de type `u` ne promeut rien. Une notice sans preuve laisse le tome détecté,
affiché avec son motif, jamais rattaché ni coté. La BNF interrogée en MarcXchange
l'est sous le schéma `unimarcxchange` — `marcxchange` est refusé par l'API avec un
diagnostic SRU ; un diagnostic ou une réponse illisible est une **vérification
impossible**, jamais une absence de notice. La limite assumée de cette preuve — une année seule ne
distingue pas un livre paru d'un livre prévu plus tard dans la même année civile —
est écrite et datée dans le module de la règle, seul endroit qui la décrit. L'[admission](../../app/domain/rules/tome_admission.py)
vérifie l'identité (ISBN, série, numéro, auteur, éditeur) avant la date et ne
remplace pas la sélection, qui dédoublonne et priorise. La carte de création ne
rattache que le tome explicitement demandé ; les autres tomes admis restent
« détectés, non ajoutés » et ne s'ajoutent que sur demande explicite. Aucun tome
sans ISBN n'est créé pour compléter un décompte annoncé.

Un ISBN ne répond pas toujours d'une seule notice, et deux notices ne se valent pas :
l'ISBN est donc jugé **entier**, jamais sur sa notice la plus favorable. Une notice qui
décrit une autre édition — `200$g` « d'après l'œuvre de » l'auteur cherché, ou une
collection `225$a` différente de celle que plusieurs tomes de la série attestent —
fait refuser le tome en `different_edition`, affiché « édition différente détectée » :
le tome reste au récapitulatif avec son motif, mais n'est jamais rattaché. Deux notices
qui se contredisent sur l'éditeur, le format (`215$c`/`215$d`) ou le type de date font
refuser l'ISBN en `notices_contradict` : aucune ne prouve plus l'édition demandée. Une
zone absente ne contredit jamais rien, et deux écritures de la même maison —
« Éditions Gallimard Jeunesse » et « Gallimard Jeunesse » — ne s'opposent pas, alors que
deux départements d'une même maison (« Gallimard jeunesse », « Gallimard bande
dessinée ») se distinguent. Illustration et dimensions se comparent **séparément**,
jamais concaténées : deux notices à « 18 cm » dont une seule précise l'illustration
décrivent le même format. Une adaptation d'un **autre** auteur ne dit rien contre le
tome : c'est le cas ordinaire d'une série de BD adaptées, veillée au nom de son
dessinateur. Il n'existe pas d'exigence d'auteur principal : l'auteur observé reste
celui que la recherche a demandé.

La collection de référence n'est retenue que si elle **l'emporte seule** : à égalité
entre collections également attestées, la série n'en atteste aucune, et rien n'est
écarté pour ce motif — départager par ordre alphabétique ferait dépendre un refus d'un
détail d'écriture.

Chaque refus d'édition laisse une ligne de journal **structurellement courte** :
`event=tome_admission`, `reason_code` (`different_edition` ou `notices_contradict`) et
`axis` (`adaptation`, `collection`, `publisher`, `format`, `date_type` ; plusieurs axes
séparés par virgule quand ils parlent ensemble). Ni ISBN, ni titre, ni auteur, ni URL :
le journal compte les refus, il ne raconte pas l'annonce. L'ajout explicite par ISBN
(`/addisbn`) n'y touche pas : il ne juge pas l'admission d'un tome et reste possible
pour l'adaptation elle-même.

Contrôles de ce paragraphe, dans [l'admission](../../tests/unit/test_tome_admission.py) :
le cas réel du tome 1 (`test_the_passe_miroir_volume_one_is_not_admitted_on_its_certain_notice`,
`test_the_passe_miroir_volume_one_is_refused_even_against_its_collection`), les axes de
contradiction et l'absence de contradiction sur une zone absente
(`test_contradictions_between_notices_are_named_by_axis`,
`test_two_notices_that_contradict_each_other_prove_nothing`), les deux départements
et les variantes d'une même maison
(`test_two_departments_of_one_house_are_not_the_same_publisher`,
`test_the_same_house_spelled_longer_is_not_a_false_refusal`), l'adaptation de l'auteur
cherché et celle d'un autre
(`test_an_adaptation_of_the_watched_author_is_another_edition`,
`test_an_adaptation_of_another_author_leaves_the_tome_alone`) et la collection attestée
par la série (`test_a_series_whose_volumes_change_collection_attests_none`,
`test_a_collection_spelled_two_ways_is_attested_as_one`,
`test_two_equally_attested_collections_attest_none`), les axes matériels comparés
séparément (`test_an_illustration_named_once_does_not_contradict_a_shared_dimension`) et
l'axe porté par le verdict
(`test_the_contradiction_axis_names_every_axis_that_spoke`). Bout en bout, sur les trois
notices réelles de l'incident : [la découverte de la
série](../../tests/unit/test_creation_series_preview.py)
(`test_the_discovery_refuses_the_adaptation_bnf_gives_for_the_first_volume`), son
affichage (`test_the_preview_names_a_different_edition_without_attaching_it`) et la
ligne de journal, sans contenu d'annonce
(`test_a_refused_edition_is_journalised_by_code_and_axis_only`). L'ajout explicite de
l'adaptation par ISBN est tenu par
[`test_addisbn_still_adds_the_adaptation_the_series_flow_refuses`](../../tests/unit/test_add_isbn.py),
qui vérifie que la règle d'admission n'est pas consultée sur ce chemin.

Une cote de création retient la moins chère des offres livrées des états acceptés
(neuf avec ou sans étiquette, très bon état) et nomme l'un de quatre états : trouvée,
aucune offre, récupération impossible, offres écartées par les filtres — avec leur
nombre et la moins chère. Le prix maximum filtre, il n'est jamais réécrit. Un prix
déjà connu n'est jamais présenté comme l'offre du jour : il s'accompagne de la
mention « dernière offre connue ». Une cote trouvée se lit « montant (marchand) »,
sans état ni mention de port. Le plus bas sur douze mois est cité sous le libellé
« Prix bas », montant seul ; l'absence de relevé et l'historique illisible sont
dits distinctement.

Contrôles : [admission](../../tests/unit/test_tome_admission.py),
[cote](../../tests/unit/test_cote_outcome.py),
[aperçu de création](../../tests/unit/test_creation_series_preview.py).

## Minimum source et fraîcheur indépendante

Le panneau conserve « Dernière offre connue » et « Prix bas » quand une
valeur existe, ainsi que le lien de l'offre du livre sélectionné. La liste de
boutiques n'est plus affichée ; ses données sources et les filtres de comparabilité
restent conservés. Sans valeur, le panneau explique l'indisponibilité sans champ
chiffré vide ni validation implicite du prix.

Les offres courantes et l'historique ont des dates de réussite distinctes. Une
réussite partielle ne rafraîchit pas la composante en échec. Une composante
qu'aucune tentative n'a demandée — une reprise ne relance que les offres
courantes — ne compte pas comme un échec : elle reste due pour la passe suivante.
Une réponse vide valide
peut dater la vérification sans effacer le dernier prix connu ; la date de ce prix
reste sa collecte réelle. « Dernière MAJ » affiche seulement la date et l'heure de
la dernière réussite connue parmi les composantes, dont les dates distinctes restent
en base. Elle ne certifie pas leur fraîcheur commune. Ni tentative ni date de
changement du prix ne remplace une collecte réussie ; sans date réelle, la ligne
est omise. Une date historique mal formée n'efface pas la date courante connue.

Chaque composante suit son propre rythme, tiré de la zone de la cote face au
plafond de l'alerte : six heures en chaud, deux jours en tiède, sept jours en froid,
trente jours pour l'historique. Une seule passe existe : elle lit les échéances
(`refresh_state`), les ordonne chaud → tiède → froid, la plus ancienne d'abord, et
n'en sert que la capacité réellement libre du régulateur. Aucune grille horaire ne
réserve de créneau, et une échéance déjà écrite n'est jamais déplacée.
« Prochaine MAJ » affiche uniquement l'échéance réellement enregistrée ; sans
échéance, la ligne est omise. Une collecte de routine ne demande qu'une composante ;
seule la création d'alerte demande les deux. Le bilan quotidien de 09:00 (Paris)
donne trois nombres — à jour, hors de leur rythme, nouveaux prix bas de la veille.
Un cache récent sans réservation exploitable est replanifié sans nouvelle collecte.
La réservation conserve son verrou jusqu’à la fin de la transaction, commit ou
annulation. Une planification ciblée ne modifie pas les échéances des autres livres.
Un report administratif encore actif garde sa reprise datée, y compris après un
historique seul, sans replanification métier ni faux succès. Après échec, les reprises
retombent simplement au rythme de leur zone ; elles conservent les valeurs
et dates de réussite. Les appels simultanés d'un même livre sont mutualisés dans
l'unique processus ; deux passes de prix ne s'exécutent pas simultanément.

Pendant la plage `NIGHT_MODE_*`, la maintenance nocturne fait un vrai tour de
nuit : une passe de la seule zone froide, avec les pauses longues entre vagues,
puis rend la main ; elle ne démarre plus de vague à la fin de la plage et le
travail restant suit les échéances persistées. Ce tour ne rend aucune composante
éligible avant son rythme et ne modifie pas les passes de jour. Il est inactif si
le mode nuit est désactivé, et les messages Telegram restent ceux des incidents et
du bilan quotidien. Sa fin de plage suit l'heure locale du fuseau configuré, comme
la pause du poller.

La migration 0015 ajoute les champs de provenance et programmation sans créer de
réussite historique. La [reprise progressive](../operations/chasse-weekly-prices.md)
nécessite un inventaire puis une autorisation explicite avant écriture réelle.
La [procédure d’accès](../operations/chasse-access.md) décrit l'authentification,
les priorités de configuration et la maintenance intégrée sans cron supplémentaire.

Une panne technique de la recherche CAL par ISBN ne déclenche pas de recherche
par titre. Le repli bibliographique est réservé aux résultats vides valides ou
aux identités rejetées ; une restriction source arrête le parcours à l'étape où
elle apparaît. Les diagnostics par étape restent internes. Une panne CAL reste
limitée aux collectes concernées ; elle ne ferme ni
l'ingestion ni Telegram. Les délais de reprise et restrictions source restent
appliqués par les chemins de collecte et de session.

Une fiche CAL déjà enregistrée est réutilisable pour le même ISBN vérifié, sans
recherche supplémentaire ni contournement des restrictions. Un corps HTTP 200 vide
reste un échec source avec métadonnées HTTP, distinct d'un résultat JSON valide
sans livre. Une réponse historique ne date la composante que si elle est
structurellement valide : objet, `stats` en liste de séries avec `label` textuel et
`statValues` en liste. Le [diagnostic ciblé](../operations/chasse-search-results.md)
distingue les observations réseau des tests isolés.

## Diagnostic, incidents et bilan quotidien

Il n'y a plus de bilan de passe : ni compteurs de traitement, ni modes, ni promesse
d'une « prochaine passe complète », ni alerte « aucune cote » ou « rafraîchissement
dégradé ». Un message immédiat ne part que pour un fait actionnable — un incident
de source durablement établi, son rétablissement durable, une passe interrompue par
une exception. Le reste est silence. Ce qui décrit l'état du marché tient dans un bilan
quotidien de trois lignes, à 09:00 heure de Paris : livres à jour sur le total,
livres hors de leur rythme, nouveaux prix bas de la veille. Les listes détaillées de
fraîcheur restent des diagnostics, écrits dans les journaux et non envoyés.
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

Une passe ne demande qu'une composante et au plus deux collections : un échantillon
en échec y vaut un taux de 1,0 sans rien prouver. Un incident se compte donc en
observations **consécutives** et en durée, jamais en taux ni en seuil de bruit
abaissé : au moins trois échecs consécutifs couvrant une heure. Le rétablissement
exige la même persistance : trois réponses consécutives couvrant une heure. Un
échec isolé, et une alternance succès/échec, ne peuvent donc produire aucun
message : l'annonce arrive au plus tôt une heure après la première tentative en
échec, et le retour au plus tôt une heure après la première réponse. C'est le prix
de la certitude, pas un délai de grâce réglable. Un message d'incident nomme la
source et l'opération, la cause lisible et l'heure de début ; les échantillons, la
fenêtre et les causes de la décision figurent dans les journaux, pour que
« pourquoi ce message ? » reste répondable.

Un échec n'accuse la source que s'il l'accuse vraiment : un délai dépassé, une
connexion impossible, un quota (HTTP 429), un défi, une authentification refusée ou
une erreur serveur. Un relevé incomplet `partial` (`incomplete_offers`, HTTP 200) et
un livre écarté (`volume_mismatch`, `identity_mismatch`, `empty_response`) prouvent
que la source répond : ce sont des relevés incomplets, jamais une panne. Une
tentative `skipped` n'est pas une observation ; elle ne juge pas la source et
n'interrompt pas la suite.

Un couple qui n'est plus tenté depuis douze heures sort de l'état sans message :
une source retirée du registre n'a ni panne ni rétablissement à annoncer, et son
état ne survit pas à sa disparition. L'absence de sollicitation ne rétablit pas une source.
Les changements non remis restent en attente de notification, avec leur nature
(incident ou rétablissement) pour qu'un incident et son retour ne s'effacent pas
l'un l'autre. Suites d'observations, couples annoncés et changements en attente
sont persistés dans la même clé Redis. Une clé portant l'état d'une autre version
est ignorée plutôt que réinterprétée, et laissée en place pour un retour en
arrière.
Si l'incident n'a jamais pu être remis et qu'un rétablissement est réellement
observé, une synthèse des deux événements reste à remettre. Elle indique que
l'incident est passé ; elle ne présente pas cette panne comme actuelle. Un échec
de remise conserve cette synthèse, y compris si les passes suivantes n'interrogent
pas la source. Une nouvelle panne avant remise conserve la synthèse précédente et
signale séparément le nouvel incident en cours. La remise confirmée retire les
changements en attente ; elle ne garantit pas une livraison exactement une fois.
L'état utilise Redis avec repli local dans l'unique processus ; le repli ne survit
pas au redémarrage. Le dédoublonnage ne dépend pas d'une clé par occurrence de
passe : c'est l'épisode qui décide, donc un incident déjà annoncé reste silencieux
et un rétablissement se dit une fois. Une nouvelle panne durable après un
rétablissement annoncé est un nouvel épisode : elle se dit.
Un échec ou une annulation d'envoi libère le créneau ; une remise Telegram ambiguë
ou partielle ne permet pas de garantir exactement un message par destinataire.

Une réponse CAL d'offres est complète quand **tous les moteurs demandés ont répondu** :
un moteur qui a répondu sans rien trouver est présent avec une liste vide, un moteur
muet est absent. La complétude se lit donc dans la différence entre les moteurs
demandés et les groupes reçus, jamais dans la présence d'un groupe particulier :
le groupe 22 n'est ni nécessaire ni suffisant. Une réponse dont l'ensemble en attente
n'est pas vide reste `partial` (`incomplete_offers`), même après les sondages bornés :
elle n'est jamais une absence confirmée, et un relevé partiel ne remplace ni les
dernières offres validées, ni leur prix, ni leur date de réussite complète. Un relevé
complet sans offre comparable est une absence confirmée, distincte d'un échec.

Le sondage reprend la fiche et ses jetons déjà obtenus, avec pour seul état nouveau
le compteur de tours et la liste des moteurs encore attendus. Il est borné en nombre
de tours et en durée, ne régénère aucun `data-hash`, ne relance aucune recherche et
s'arrête sur restriction source. Un schéma invalide reste un échec technique. Chaque
tentative conserve le nombre de moteurs demandés et reçus, le nombre de sondages et
leur durée, afin de rendre la mesure ultérieure possible sans nouvelle instrumentation.
L'historique source peut réussir indépendamment.
