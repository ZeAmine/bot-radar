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
aucune offre datée, composition inconnue ou catalogue épuisé. Un panneau ouvert peut
montrer une collecte en cours ; un message envoyé reste un instantané.
Le panneau partagé relit immédiatement le cache, conserve le tome sélectionné,
actualise progressivement et invalide le contexte après fermeture (ADR-042).

Contrôles existants : [offres](../../tests/unit/test_market_offer_rules.py),
[comparateur](../../tests/unit/test_market_comparator.py),
[séries](../../tests/unit/test_series_matching.py),
[validité de lot](../../tests/unit/test_lot_reference_validity.py),
[cycle du panneau](../../tests/unit/test_market_panel_lifecycle.py).
SQLite ne prouve pas les verrous PostgreSQL. Aucune lecture documentaire ne prouve
une collecte réelle ; les procédures de session et leurs permissions restent distinctes.
