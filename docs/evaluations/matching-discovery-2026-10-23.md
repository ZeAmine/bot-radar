# Matching et découverte — 23 octobre 2026

Statut : code préparé pour revue ; contrôles finaux consignés dans la PR.
L’inventaire de dissociation reste non exécutable avec le schéma actuel. Aucune écriture métier, règle utilisateur,
formule, configuration, migration ou dépendance modifiée. Aucun merge ni déploiement.

## Points de décision

| Entrée | Niveau comparé | Preuve et limite |
| --- | --- | --- |
| Matcher et diagnostic | Exemplaire annoncé / demande, tome et manifestation | ISBN vendeur contradictoire rejeté ; titre brut conserve les compositions ; couverture complète multi-tomes |
| Catalogue par titre/auteur et découverte de série | Œuvre, expression et tome | Titre cohérent + auteur attendu complet + langue française ; homonymie non résolue = abstention |
| Vérification ISBN | Manifestation | Autorité bibliographique ; ne prouve ni l’intention utilisateur ni le contenu réel de l’annonce |
| Cache et confirmation après Telegram | Manifestation liée à l’alerte courante | ISBN déclaré filtre le cache ; identité et version revalidées dans une autre session ; aucune liaison sans preuve |
| Préparation de création | Œuvre puis manifestation choisie | Candidats non confirmés distincts des ISBN confirmés ; aucune absence de catalogue déduite d’une identité incertaine |

## Jeu et mesure

Base de code : `0b119ff67c802481aca4027624fd68077efa28b6`, archivée séparément.
[Corpus versionné](identity-reference-v1.json), SHA-256
`58acb4d96b81c401271352f60758c99dc498fda13c341efb070cd0deee891f15`.

Les 451 cas comprennent les 299 observations brutes à autre ISBN (63 négatives,
236 ambiguës), 102 associations positives encore présentes après les corrections,
les trois reproductions initiales, trois contre-exemples synthétiques et 22 notices
correctes rejouées avec puis sans auteur attendu. Les dates et empreintes des sources
figurent dans le corpus ; aucun export brut, identifiant utilisateur ou prix de
production n’est versionné. Les paires observation/livre sont des rejouements
contrefactuels du matcher : elles ne reproduisent pas le fournisseur marché original.
Les cas catalogue utilisent les vrais services et doublent les frontières fournisseur.

Le premier noyau et sa mesure précèdent les correctifs ; la référence a ensuite
été enrichie pour les positifs actuels et les contre-exemples de revue. Les mesures
comparatives rejouent toujours le même corpus final sur la base immuable et le diff.
Les étiquettes sont une analyse d’agent des preuves, pas une validation utilisateur.

| Mesure sur les 215 cas binaires | Base | Correctif |
| --- | ---: | ---: |
| Vrais positifs | 146 | 124 |
| Faux positifs | 46 | 0 |
| Vrais négatifs | 23 | 69 |
| Faux négatifs | 0 | 22 |
| Précision | 76,04 % | 100 % |
| Rappel | 100 % | 84,93 % |
| Ambigus acceptés / 236 | 201 | 0 |

Les 22 bonnes notices perdues sont les mêmes identités françaises correctes rejouées
sans auteur attendu. Leur identité réelle reste étiquetée positive ; leur contexte
d’entrée ne permet pas de choisir entre homonymes, donc le correctif s’abstient.
Chaque perte garde son ISBN, son titre et sa justification dans le corpus et le
rapport privé. Les 22 contreparties avec preuve passent. Aucune prétention de
précision globale ou de rappel des annonces réelles manquées n’est tirée de cet
échantillon difficile. Aucun seuil numérique nouveau n’a été choisi à l’intuition.

## Règles et données

L’audit privé couvre 134 alertes et 675 règles : 673 suggestions, deux améliorations
probables sur probes synthétiques ; aucune erreur d’annonce réelle démontrée.
Chaque ligne conserve avant/après identiques, effet du retrait de la règle et preuve.
Les probes viennent des titres de livres conservés ; ils ne mesurent pas le rappel
réel des annonces manquées. Aucun lot de règles n’est proposé ni appliqué.

L’inventaire privé part des 63 observations démontrées : 28 encore présentes dans
l’instantané ultérieur, 35 hors population donc exclues, sans conclure à leur suppression.
Les mauvaises associations d’alertes initiales sont déjà absentes de cet instantané.
Le champ `MarketPriceSnapshot.book_id` est non nullable : dissocier les 28 observations
nécessite une décision de schéma distincte. L’inventaire est préparé et empreinté,
mais **non exécutable et non prêt à approuver**. Aucun effacement ni contournement
par les critères de comparabilité n’est proposé.

## Tests et arbitrage

Les preuves rouges couvrent ISBN, homonymie/auteur, tome, composition, langue,
cache contradictoire, fallback, confirmation après Telegram et reprise. Des cas
paramétrés couvrent casse, accents, ponctuation et alertes sans livre. Hypothesis
n’est pas installé ; aucune dépendance n’a été ajoutée. Les contre-exemples de revue
protègent aussi les listes romaines, la notation `1/5` et les mots suivant un tome.

Les deux scénarios E2E du backfill ont été adaptés sur autorisation explicite :
`--author UUID=AUTEUR` fournit transitoirement l’auteur attendu par alerte, transmis
aux chemins réels de découverte et liaison. Chaque scénario distingue preuve
explicite et abstention sans preuve. Les 10 assertions originales de première passe
et les 3 de seconde passe, dont l’idempotence, sont conservées à l’identique.
Les langues sont fournies par les réponses HTTP factices BNF et édition OpenLibrary.

Le benchmark d’ingestion lente ne contient ni ISBN d’alerte, ni ISBN vendeur,
ni livre lié, ni auteur attendu ; les inclusions ne désignent que « the grocery ».
Son annonce désigne une intégrale sans identifier ses éditions. Les six notices
numérotées, leurs auteurs et leur langue sont une réponse fournisseur simulée,
pas une preuve de l’intention ni de la composition exacte demandée. Leurs codes
simulés ne sont reliés à aucun ISBN attendu. Les six associations ne sont donc
pas démontrées dans ce contexte précis.

Sur accord explicite, **seule l’assertion finale de ce scénario passe de six à zéro
liaison**. Les six autres assertions, l’envoi Telegram, l’ordre, les budgets et tous
les autres benchmarks restent identiques. L’abstention ne bloque pas la notification.
Le benchmark positif distinct passe un auteur explicite au vrai chemin de liaison
et conserve six liaisons, six cotations, ses nettoyages entre vols et son budget.

Un test ciblé additionnel exerce le vrai résolveur avec un ISBN français concordant
en cache et aucun auteur : deux résolutions successives rendent la même édition,
un seul lien est relu dans une nouvelle session et aucune découverte n’est autorisée.
Il est rejoué sur SQLite et PostgreSQL. L’absence d’auteur n’implique donc pas une
abstention lorsqu’une autre preuve suffisante, ici l’ISBN attendu exact, existe.

La transmission proposée depuis la préparation a été abandonnée sans modifier
l’application : toute préparation bloque l’envoi et elle est effacée à l’activation.
La garde, son effacement et le chemin avant Telegram sont inchangés. Cette proposition
n’a introduit aucune modification applicative à retirer.

La revue distincte est une revue d’agent, sans validation humaine. Le plan des prix
est préparé hors réseau ; prix actuels non certifiés, 88 livres différés et CAL
suspendu. Les acquis précédents sont conservés. L’inventaire reste bloqué par le
schéma, sans écriture métier, migration, merge ou déploiement.

### Preuve historique du premier commit `8318011`

- `ruff check .` : vert.
- `python -B scripts/verify_consolidation.py typecheck` : vert, 169 fichiers applicatifs puis 8 fichiers d’outillage.
- `make test-isolated PYTHON=<venv>/bin/python` : 3083 succès, 124 ignorés, 3 échecs ; les trois consommateurs incompatibles nommés ci-dessus. Aucun test nouvellement ignoré.
- 331 tests ciblés identité, volumes demandés et matching de série : verts après les derniers contre-exemples.
- PostgreSQL jetable : 150 succès sur les sept fichiers concernés (ISBN vendeur, sérialisation ingestion, préparation, totaux de série, retrait ISBN, couverture planifiée, latence). Migrations existantes et isolation vérifiées ; ressource jetable supprimée par le lanceur.
- Liens locaux modifiés, diff et index : contrôlés ; revue distincte statique sans défaut démontré restant hors arbitrage legacy.

Ces résultats historiques décrivent le premier commit avant les accords ciblés.
Les résultats de la tête qui inclut ces accords figurent dans la PR et le suivi
unique, avec le SHA et la sélection exacte des contrôles. Ils ne certifient aucune
production ; les exceptions de test autorisées restent tracées ci-dessus.

### Validation finale après les accords ciblés

- `ruff check .` et mypy isolé : verts (169 fichiers applicatifs et 8 d’outillage).
- `make test-isolated PYTHON=<venv>/bin/python` : **3089 succès, 124 ignorés**, quatre avertissements ; aucun test nouvellement ignoré.
- PostgreSQL jetable : **218 succès**, incluant le nouvel ISBN sans auteur, sérialisation, versions/sessions, idempotence, reprise des notifications et du poller, réservation concurrente, persistance de livraison et latence. Les migrations existantes passent et le lanceur supprime sa ressource.
- Scénarios négatif, positif, ISBN et backfill ciblés : 31 succès, quatre cas nécessitant PostgreSQL ignorés sous SQLite puis couverts par la passe PostgreSQL.
- Rejeu des 451 cas et audit des règles : métriques inchangées, aucune règle utilisateur modifiée.
- Revue distincte du dernier diff : aucun défaut trouvé dans le périmètre autorisé ; revue statique d’agent, sans validation humaine.

La seule exception d’assertion autorisée est celle décrite ci-dessus. Aucun changement
applicatif n’est introduit par la proposition abandonnée depuis préparation.
Les résultats CI du commit exact sont suivis dans la PR ; un résultat local ne
certifie pas la production. L’inventaire non exécutable reste le seul blocage de
préparation des écritures métier, qui ne sont pas appliquées.
