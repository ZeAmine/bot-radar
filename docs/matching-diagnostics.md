# F10 — expliquer une décision et évaluer des exemples

Statut : contrat actuel du diagnostic et du corpus, avec mesures historiques. Périmètre : explication locale ; aucune validation indépendante des attentes métier.

Le diagnostic explique le comportement du code. L'attendu métier indique ce que
l'utilisateur souhaite. Un test protège un comportement convenu ; une étiquette
humaine valide un exemple indépendamment du code. Ces quatre objets restent séparés.
Le lot historique F10 ne modifie aucune règle, seuil, politique ISBN/langue ou cote (ADR-003, ADR-021,
ADR-029 et exception d'édition ADR-038 conservés).

## Explication dans le résultat réel

`evaluate_listing_against_alert` conserve décision, raison française, score,
indicateurs et ordre des règles. Il ajoute les informations pendant la même passe,
sans rejouer de prédicat. L’ingestion utilise directement ce résultat.
Il n'y a ni journal par paire dans l'ingestion, ni stockage supplémentaire.

| Code stable | Première étape décisive |
| --- | --- |
| `alert_inactive` | Statut |
| `category_mismatch` | Catégorie |
| `price_above_max` | Prix |
| `volume_conflict` | Tome individuel contredit par le tome ou la composition annoncée |
| `volume_coverage_missing` | Couverture complète des tomes demandés non établie |
| `isbn_conflict` | ISBN vendeur valide différent de celui demandé |
| `isbn_keywords_missing` | ISBN absent et critères du repli manquants |
| `isbn_fallback_rejected` | Repli ISBN/titre non satisfait |
| `default_exclusion` | Exclusion de catégorie |
| `user_exclusion` | Exclusion utilisateur |
| `gaming_language_rejected` | Langue gaming |
| `required_keywords_missing` | Inclusions et variantes |
| `condition_rejected` | État |
| `accepted` | Toutes les étapes franchies |

`unexplained` est uniquement le défaut d'un résultat construit hors du matcher réel.
Les voies d'acceptation sont `includes`, `variant`, `unconstrained`, `isbn_text`, ou
`isbn_field/…` et `isbn_hybrid/…` combinés avec la voie d'inclusion. Elles décrivent
les contrôles réellement franchis, pas une confirmation bibliographique.

Les éléments décisifs sont des index à partir de zéro : premier terme requis
manquant, variante utilisable retenue, exclusion dans la liste étendue. Les index
de variantes correspondent à `usable_variants`, ceux d'exclusions à la liste
multilingue filtrée, pas nécessairement aux positions du formulaire initial.
Les contenus ne sont pas copiés dans ces éléments.

Les étapes sont `status`, `category`, `price`, `isbn`, `default_exclusions`,
`user_exclusions`, `gaming_language`, `includes`, `condition`. « Évaluée » signifie
que la garde de cette étape a été atteinte, même si elle n'a aucune contrainte
applicable. `includes` désigne le contrôle final : le repli ISBN peut consulter des
mots-clés auparavant, dans l'étape `isbn`. Une étape ultérieure au premier rejet est
« non évaluée », jamais présumée réussie. Les anciens indicateurs booléens gardent
leurs valeurs historiques ; le score reste le nombre d'inclusions configurées,
même en cas de variante, et n'est pas une probabilité.

## Outil local

Depuis la racine du dépôt, avec l'environnement Python déjà disponible :

```sh
python scripts/matching_diagnostics.py explain /chemin/pair.json
python scripts/matching_diagnostics.py evaluate docs/evaluations/matching-synthetic-v1.json
```

Un couple explicite comporte `alert` et `listing`. Leurs champs sont ceux du corpus
ci-dessous ; un objet `cases[0].pair` extrait manuellement peut servir d'entrée.
Chaque couple reçoit un seul appel au matcher réel. Le titre est normalisé avec
le helper existant ; le titre brut est aussi transmis pour préserver les plages
de tomes avant normalisation. Les autres critères sont fournis explicitement. Il n'y a aucune
lecture DB, collecte, confirmation ISBN, découverte de fichier `.env` ou chargement
de configuration. L'URL d'entrée est consultable par la règle ISBN existante, sans
requête réseau. Les données doivent représenter l'instantané voulu, pas seulement
l'alerte actuelle supposée identique à une ancienne.

Toute sortie porte `mode: "rejeu"`. Elle ne retrouve pas la décision originale d'un
cas passé dont l'instantané ou les critères ne sont plus disponibles. Les données
de comparaison F03-B et les chronomètres F05 ne sont pas des traces de décision.

Les rapports utilisent une liste blanche : codes, voies, index, étapes, score et
indicateurs. Ils ne rendent ni nom, titre, description, ISBN, URL, identifiant
utilisateur, payload, raison française libre, ni chemin de fichier. Les erreurs
de lecture/validation rendent seulement `invalid_input` (code de sortie 2).
Les entrées sont bornées à 1 Mio, 200 couples, 32 termes par liste, 128 caractères
par terme et 4096 caractères par texte. Les limites concernent l'outil local,
pas les règles ou contrats de l'ingestion. Les résultats restent bornés par ces
effectifs. Code 0 signifie outil exécuté, pas absence de désaccord.

## Corpus version 1

Un objet possède `version: 1` et `cases`. Chaque cas possède :

- `origin` : `synthetic` ou `provided` (fourniture explicite, aucune collecte) ;
- `pair.alert` : `name`, `category_slug`, éventuellement `status`, `isbn`,
  `include_keywords`, `variants`, `exclude_keywords`, `max_item_price_cents`,
  `accepted_conditions` ;
- `pair.listing` : `title`, `category_slug`, `item_price_cents`, éventuellement
  `description`, `condition`, `isbn13`, `url` ;
- `annotation.expected` : `accepter`, `rejeter`, `indetermine`, `non_etiquete` ;
- `annotation.source` : `business_test`, `human_validated`, `none` ;
- `annotation.reviewed` : `true` seulement pour `human_validated`.

Sans annotation, le cas est non étiqueté. Une étiquette exige une source, et une
validation humaine exige le marqueur de revue explicite. Ce marqueur est une
déclaration de provenance, pas une preuve automatisée de la revue. Aucun outil
ne remplit l'étiquette depuis la décision du matcher. Les champs inconnus, états
incohérents et tailles excessives sont refusés sans afficher leur contenu.

Les matrices séparent `business_test` et `human_validated` : lignes = attendu,
colonnes = décision. Les indéterminés et non étiquetés sont comptés séparément et
exclus des dénominateurs binaires. La précision d'échantillon vaut TP/(TP+FP), le
rappel TP/(TP+FN) ; numérateur, dénominateur et valeur figurent ensemble. Un
dénominateur nul donne `null`, jamais 0 ou 100 %. Les désaccords sont référencés par
index de cas, sans reproduire les données sensibles.

Un corpus sélectionné pour ses difficultés ne fournit aucune précision globale,
aucun taux de faux positifs de production ni mesure représentative des annonces
manquées. Les signalements « Faux positif » seuls ne fournissent pas le rappel.
Les 139 exemples avant/après d'inclusion présents dans `artifacts/` ne sont pas
importés : leurs sorties de code ne constituent pas des étiquettes humaines.
L'évaluation bibliographique du 8 octobre n'évalue pas ce matching.

## Petit lot proposé à revue humaine séparée

Le fichier `matching-synthetic-v1.json` contient dix exemples fabriqués, sans donnée
collectée. Sept attendus binaires sont des hypothèses métier de test écrites
explicitement ; un cas est indéterminé et deux sont non étiquetés. Aucune de ces
étiquettes n'est présentée comme validée par l'utilisateur.

La revue peut commencer par cinq cas (index à partir de zéro) :

| Index | Question à trancher sur le seul exemple |
| --- | --- |
| 3 | Une variante textuelle suffit-elle à rendre ce couple pertinent ? |
| 4 | Ce repli de titre sans code vendeur convient-il à l'intention d'édition ? |
| 7 | Un code cité à côté d'un autre titre permet-il de juger l'annonce ? |
| 8 | Cette autre édition doit-elle être acceptée pour cette demande ? |
| 9 | Ce lot mélangé est-il pertinent pour cette demande ? |

Une revue peut conserver `indetermine` : on ne force pas une vérité binaire.
Elle ne modifie aucune règle ; toute règle/seuil demandé exige un arbitrage séparé.

## Vérification

Les tests couvrent premier rejet, voies d'acceptation, étapes non atteintes,
expurgation, appel unique, import sans settings/réseau, corpus invalide, limites et
séparation des étiquettes. Une empreinte des cinq sorties historiques sur 18144
combinaisons a été capturée depuis le code inchangé de `0cb54b7`,
indépendamment des sorties F10 : elle sert
uniquement à la parité, sans étiquette de pertinence dérivée du matcher.
Les six benchmarks existants restent inchangés ; le surcoût est mesuré localement,
sans promesse de coût nul ou mesure de production. Le nouveau script est typé dans
`make lint`, `make verify` et la CI ; aucun changement de dépendance ou migration.

### Comparaison locale base F05 / F10

Trois exécutions séparées de chaque version, mêmes outils et garde-fous, aucune
suite concurrente : SQLite en mémoire et réseau doublé. Les nombres ci-dessous
sont les médianes des trois p95, sauf le matching CPU (médiane des trois médianes).
Tous les seuils existants sont conservés et les six benchmarks passent à chaque
exécution.

| Scénario | Base `0cb54b7` | F10 |
| --- | --- | --- |
| Cas d'usage complet avec doubles | 7,77 ms | 8,02 ms |
| Doublon | 5,09 ms | 5,05 ms |
| Matching CPU par couple | 23,6 µs | 23,8 µs |
| Cas d'usage de série avec doubles | 3,65 ms | 3,62 ms |
| Sélection de série | 1,22 ms | 1,22 ms |
| Liaison de série après envoi | 17,40 ms | 18,01 ms |

Les p95 du cas d'usage complet F10 varient de 7,92 à 12,61 ms, contre 7,55 à
7,96 ms sur la base. La liaison varie de 17,12 à 21,32 ms, contre 17,35 à
19,18 ms. Ces exécutions décrivent le coût observé et sa variabilité, sans isoler
une cause pour les écarts ni promettre un surcoût nul. Elles ne mesurent aucune
livraison Telegram réelle ni performance de production.

## Référence d’identité du 23 octobre 2026

[Le bilan matching/découverte](evaluations/matching-discovery-2026-10-23.md)
distingue les rejouements d’annonces contrefactuels des décisions de catalogue.
Son corpus `identity-reference-v1.json` a un schéma spécialisé ; il est rejoué par
`tests/unit/test_identity_reference.py`, pas par la commande `evaluate` F10.
Ses étiquettes ont été préparées par l’agent depuis les preuves conservées ; elles
ne sont pas une validation humaine ni une mesure représentative de production.

Le diagnostic fournit au domaine le titre brut et l’extraction locale de tome en
amont, comme l’ingestion. Aucun historique notifié n’est disponible dans les exports
accessibles du mandat ; ce corpus connu ne constitue pas un contrôle indépendant.
