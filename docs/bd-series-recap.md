# Récap BD : séries multi-tomes

Correction sur `fix/bd-series-recap`, depuis `main` / `origin/main` vérifiés au commit
`a6a1325c0f2c178446ec28ba4afc22c6244910b8`.

## Diagnostic en lecture seule

La capture Bird King montre six blocs (Volume 1–3, puis Livre 1–3), aucun ISBN par
bloc, aucun tome 4 et une identité incomplète. Le script isolé sur cette révision
reproduit les six notices avec une BNF simulée (trois volumes) et OpenLibrary simulé
(trois livres avec d'autres ISBN, et un quatrième sans ISBN). Les transports réels
sont refusés et les fichiers `.env` / secrets-directory sont neutralisés.

* La découverte dédupliquait les ISBN bruts, mais ne sélectionnait pas une édition
  par numéro. La confirmation affichait chaque livre lié avec son titre brut : deux
  sources ou traductions produisaient deux vocabulaires et deux blocs par numéro.
* `BookResolution.editions()` ne construisait pas ces blocs. Ses éditions concurrentes
  restent distinctes des tomes de `SeriesVolume` ; `notice_titles` et `series_signal`
  conservent le rôle de preuve de série défini par ADR-025.
* Une notice sans ISBN était éliminée. Le double contenant Livre 4 sans ISBN est
  absent du catalogue rendu, alors que le signal pur reconnaît les numéros 1–4.
  Le SRU lisait également une seule page de dix notices, et cette découverte
  n'utilisait pas Google Books. Ces mécanismes sont prouvés ; sans les réponses
  originales, aucun ne peut être désigné comme la cause historique certaine du tome 4.
* La projection série effaçait auteur et éditeur ; le gabarit supprimait sa langue
  et n'affichait pas année, format et source. Les deux problèmes se cumulaient.

## Comportement corrigé

La découverte de création est explicitement distinguée du repli post-alerte. Elle
page la BNF, complète par OpenLibrary puis Google Books configuré et conserve les
numéros sans ISBN. Le sous-titre Google peut porter le numéro. Les candidats de repli
restent MEDIUM ; un ISBN de tome affiché confirmé passe par `verify_isbn`, avec
notice BNF, code, titre, numéro et auteur cohérents. Aucun seuil de titre n'est réduit.

`select_series_volumes` normalise ISBN-10/13, élimine les doublons puis choisit une
édition par numéro, français confirmé en priorité, et trie les numéros. Le rendu
utilise un seul vocabulaire provenant des notices retenues. Les limites de découverte
sont dites, le total inconnu n'est jamais déduit du plus grand numéro. Un total
explicitement annoncé permet de représenter ses numéros manquants sans fausses notices.

L'identité BD est commune au one-shot et à la série : langue, auteur, éditeur, année,
format, source et mots-clés. Les champs absents valent « Non renseigné », y compris
l'identité seulement supposée par le moteur. Une série ne porte aucun ISBN global ;
chaque tome possède sa propre ligne d'ISBN, de provenance et de cote. Une année de
publication d'un tome n'est pas transformée en année de naissance de la série.

La lecture de cote passe par `get_comparison_summary(..., for_creation=True)` et
le cache local. Les formules, `market_offer.py` et ADR-016 restent inchangés. Les
prix et le minimum annuel appartiennent toujours à l'ISBN retenu. Aucun prix, ISBN
ou auteur absent n'est inventé ; une passe vide est rendue comme absence d'offre datée.

Les confirmations initiales sont liées avant le commit en pause et conservées dans
la demande JSON de préparation. La préparation transmet ses nouvelles preuves à la
confirmation et réconcilie ses liens avec la sélection finale ; les livres et leurs
historiques ne sont pas supprimés. Une panne à la seconde découverte ne perd pas les
ISBN déjà confirmés. Une reprise des mêmes critères conserve ces preuves ; un
changement d'identité les invalide. Les titres numérotés attestés réalignent les
anciennes fiches de même ISBN. Aucune migration SQL n'est nécessaire.

## Fichiers concernés

* Rendu et parcours : `app/bot/handlers/alerts_helpers.py`, `alerts_create.py`,
  `alerts_create_confirm.py`.
* Autorité et contrats : `app/services/book_metadata_service.py`,
  `app/use_cases/book_search.py`, `app/providers/google_books.py`.
* Création naturelle : `app/services/ai/modules/natural_add.py`, `app/services/ai/schemas.py`.
* Préparation et liens : `app/domain/entities/alert_preparation.py`,
  `app/db/repositories/alert_repo.py`, `app/use_cases/prepare_bd_alert.py`,
  branche de création de `app/use_cases/ingest_listing.py`.
* Présentation du cache : `app/services/market_price_service.py`.
* Régressions nouvelles : `tests/unit/test_series_creation_recap.py`.
  Les doubles existants acceptent l'option additive de découverte ; les assertions
  de langue/identité sont adaptées au comportement demandé. Aucun test supprimé.

## Vérifications et portée

Les nouveaux tests vérifient quatre lignes ordonnées, ISBN normalisés, vocabulaire
unique, édition française, pagination BNF, complément Google, nombreuses éditions
OpenLibrary, absence d'ISBN/cote, absence d'offre datée, limites et total annoncé.
Les parcours SQLite exécutent la vraie création et la vraie préparation avec la
collecte doublée : ils contrôlent pause avant cotation, cache relu, activation,
exactement quatre liens et quatre ISBN, confirmations tardives, changement EN→FR,
panne de seconde découverte, ancien titre sans numéro et reprise durable.

Les tests de bascule d'édition et d'ISBN explicite contrôlent aussi année, format,
source et conservation du numéro demandé. Les régressions d'ambiguïté et de matching
existantes sont conservées. Une revue indépendante en lecture seule a conduit aux
régressions de préparation/reprise supplémentaires.

Commandes de validation :

```text
.venv/py312/bin/ruff check .
.venv/py312/bin/mypy app scripts/matching_diagnostics.py
.venv/py312/bin/python -m pytest -q
make verify PYTHON=.venv/py312/bin/python PYTEST=.venv/py312/bin/pytest \
  RUFF=.venv/py312/bin/ruff MYPY=.venv/py312/bin/mypy
```

Localement, pytest et make verify sont lancés par le harnais temporaire
`/private/tmp/bd-series-test-harness/run.py` : environnement factice équivalent aux
quatre tokens de CI, neutralisation des `.env` et secrets-directory avant imports,
et refus des connexions IP réelles hérité par les sous-processus Python. Les mocks
HTTP et SQLite en mémoire restent utilisables. Les résultats et SHA de livraison
sont consignés dans la PR.

Aucune collecte réelle, observation sur Telegram, modification de production,
exécution de PostgreSQL local, merge ou déploiement ne fait partie de ces preuves.
La CI rejoue les tests d'intégration sur PostgreSQL. La cause historique précise du
tome 4 absent demeure inconnue ; le catalogue d'une autorité peut réellement être
incomplet, et le récap l'indique au lieu d'inventer un ISBN ou une complétude.
