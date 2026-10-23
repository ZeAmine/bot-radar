# Matching et découverte — 23 octobre 2026

Statut : partiel, validation finale et livraison conditionnées aux contrôles et à
l’arbitrage des consommateurs historiques. Aucune écriture métier, règle utilisateur,
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

Trois scénarios historiques exigent une association sans auteur attendu : deux E2E
du backfill et le benchmark de recherche lente. Leur adaptation nécessiterait le
script sensible `scripts/backfill_series_tomes.py`, explicitement protégé par
AGENTS.md, et un arbitrage sur la portée de leurs tests. Ils restent inchangés,
sans suppression, mock compensatoire ni baisse de seuil. Proposition bornée dans
le dossier privé `chantier-1/arbitrage-backfill.md`. Ils empêchent d’attester une
branche verte. Les autres fixtures positives ajoutent seulement leur preuve de
langue ou d’auteur explicite et conservent leurs assertions.

La revue distincte est une revue d’agent, sans validation humaine. Les résultats
finaux, SHA, CI et limites de livraison sont consignés dans la PR et le suivi unique.
La préparation du chantier prix demeure différée jusqu’à l’étape 7 ; prix actuels
non certifiés, CAL suspendu et acquis antérieurs conservés.

### Résultats locaux du diff final

- `ruff check .` : vert.
- `python -B scripts/verify_consolidation.py typecheck` : vert, 169 fichiers applicatifs puis 8 fichiers d’outillage.
- `make test-isolated PYTHON=<venv>/bin/python` : 3083 succès, 124 ignorés, 3 échecs ; les trois consommateurs incompatibles nommés ci-dessus. Aucun test nouvellement ignoré.
- 331 tests ciblés identité, volumes demandés et matching de série : verts après les derniers contre-exemples.
- PostgreSQL jetable : 150 succès sur les sept fichiers concernés (ISBN vendeur, sérialisation ingestion, préparation, totaux de série, retrait ISBN, couverture planifiée, latence). Migrations existantes et isolation vérifiées ; ressource jetable supprimée par le lanceur.
- Liens locaux modifiés, diff et index : contrôlés ; revue distincte statique sans défaut démontré restant hors arbitrage legacy.

Ces résultats ne constituent pas une validation finale verte : les trois échecs
empêchent de déclarer le chantier terminé ou de demander le merge. Les résultats
PostgreSQL et CI exacts du commit livré figurent dans la PR et le suivi privé.
