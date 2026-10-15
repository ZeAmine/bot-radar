# Alertes, minimum annuel et fraîcheur — livraison locale du 14 octobre 2026

## Mandat et état

Bilan de l’implémentation du 14 octobre, avant la livraison autorisée le 15 octobre.
Les résultats ci-dessous sont attribués à cet arbre initial ; les preuves de livraison
sont rapportées séparément. Branche
`fix/alert-freshness-market`, worktree
`/Users/aminezegmou/Projets/bot-radar-worktrees/alert-freshness-market`.
Base initiale : `6292e2efcf754b2f88544c620fc6943fb7f3fc56` (référence locale).
Les modifications checkout du dossier principal ont été conservées à leur place.
Migration additive `0014_alert_market_freshness`, à appliquer avec l'image cible.
À la date de cette implémentation, les règles exigeaient une revue humaine des
changements du chemin de notification, du scraper et de comparaison marché. Les
obligations actuelles sont définies dans [AGENTS](../AGENTS.md#convention-de-travail).

## Modifications

- Premier catalogue Vinted réussi silencieux pour chaque nouvelle alerte, même si
  ses identifiants sont déjà connus globalement. Exclusion persistante, reprise
  après redémarrage et abandon des notifications différées correspondantes.
  Une collecte échouée ne termine pas l'initialisation. Une publication explicitement
  datée avant la création est refusée. Les alertes anciennes restent inchangées.
- `/list` : numéro, icône, titre en gras, prix compact puis statut parenthésé ;
  centimes conservés lorsqu'ils existent, HTML échappé, boutons et pages préservés.
- Une seule ligne de minimum annuel. Les points historiques fournisseurs sont
  conservés séparément des offres disponibles, avec leur date réelle, montant
  livré et provenance. Les agrégats sans date ne constituent pas une preuve annuelle.
  Même filtre d'occasion, état et marchand ; frais inchangés. Les séries passent
  par le calcul central. Les données anciennes et les preuves passées restent conservées.
- Livres et tomes des alertes actives **et en pause**, ouvrages dédupliqués ; alertes
  supprimées exclues. Résolution des rattachements manquants sans activer les pauses.
  Les lots sont dimensionnés à la population et ordonnés par tentative persistée,
  avec repos des échecs. Une erreur interne avant résultat acquiert aussi son tour.
- Tentative, collecte vide réussie, résultat partiel et échec restent distincts.
  La fraîcheur des offres utilise une réussite explicite ; celle de l'historique
  utilise une récupération qualifiée distincte. Les anciens prix restent conservés
  après échec, sans nouvelle date fictive. Les avertissements portent sur un vrai
  retard de collecte ou de preuves historiques, sans demander qu'un minimum soit égalé.

## Causes établies et données accessibles

La rafale résultait de l'absence de contrôle temporel par alerte : un identifiant
inconnu du bot était considéré nouveau. Les sélections prix excluaient les alertes
en pause et sans livre rattaché. Le warning historique dépendait de l'ancien
agrégat plutôt que de la fraîcheur réelle des données nécessaires au calcul.

Lectures de production du 14 octobre, sans modification : le scheduler a bien
traité les six tomes de L'Arabe du futur à 20 h 45 Paris ; quatre résultats avec
prix, deux collectes dégradées/échouées (tomes 3 et 6 conservant les relevés du
7 octobre). Les échecs nommaient notamment Gibert, Momox, Recyclivre et une
limitation Chasse-aux-livres. Il ne suffisait donc pas d'attendre ni de demander
que les nouveaux prix égalent les minima. 129 alertes livres actives et 12 en pause
étaient présentes ; ces comptages ne prouvent pas un état futur de la production.

Pour Grimr, 545 centimes figurent dans le payload de l'agrégat Chasse, sans date
historique du point ni preuve de son montant livré. Les dates de collecte d'octobre
ne sont pas la date du prix. Le minimum annuel local vérifiable est 1 103 centimes,
observé le 20 septembre 2026. Une interrogation de la série fournisseur a renvoyé
HTTP 200 avec corps vide : aucun point ne permet de confirmer 5,45 € dans la fenêtre.
Le code traite désormais cette réponse comme un échec historique, au lieu de
prétendre que la récupération annuelle a réussi. Les tests prouvent l'inclusion
correcte d'un point 5,45 € daté et admissible **synthétique**, pas sa présence réelle.

Le jeton Chasse était présent dans Redis et réellement utilisé ; aucune injection
corrective n'était justifiée par cette observation. Le cron de renouvellement est
installé à 02 h, avec contrôles récents. `scripts/sync_chasse_session.py` valide puis
injecte un jeton ; il ne collecte pas toutes les alertes. Sa présence ne prouve
ni une série historique exploitable ni la réussite de tous les fournisseurs.

## Déclencheurs réels après correction

La création BD prépare les rattachements et attend la cotation centrale. Le bouton
« Prix du marché » peut déclencher une collecte centrale attendue ; une lecture
cache seule n'interroge pas les fournisseurs. Après l'envoi d'une annonce sans cote
utilisable, la collecte de fond sert les annonces suivantes, sans HTTP préalable
à Telegram. Les prix et preuves annuelles se consolident lors de ces écritures ;
la projection annuelle expire selon les dates des observations, même sans nouveau minimum.

Le scheduler unique conserve les passes complètes de 07 h / 19 h Europe/Paris,
les lots à :15 / :45 et une reprise immédiate du lot au démarrage. Cible de réussite
24 h, avertissement après 48 h, concurrence 4 et pauses fournisseurs conservées.
Les rattachements manquants sont repris avant la sélection, sans lancer une
cotation massive en dehors des vagues bornées. Les collectes déjà en cours sont
mutualisées. Une identification impossible reste signalée et ne vaut pas couverture.

« Actualisation impossible » correspond à une collecte sans résultat actuel
exploitable en échec ; les derniers relevés sont conservés. Une source en panne
avec d'autres offres réussies constitue un résultat partiel. Les erreurs journales
identifient timeout, limitation/statut HTTP, réponse invalide ou exception interne
sans exposer les valeurs sensibles.

## Validation et limites

Vérifications dans une venv dédiée issue du verrou, environnement factice vidé,
aucun `.env` réel et transports externes refusés (sous-processus compris). Tests
ciblés et régressions : baseline par alerte, publications, recovery, deux statuts,
prix/HTML, preuves annuelles et expiration, rétention après échec, pause/séries,
résolution, fraîcheur vide réussie et équité sur 120 livres en plusieurs lots.

Commandes finales : `pytest -q`, `ruff check .`,
`mypy app scripts/matching_diagnostics.py` ; résultats sur l'arbre final :

- **1 920 passed, 12 skipped, 4 warnings**, en 40,39 s. Avertissements de
  dépréciation Starlette / HTTP 422 et configuration Alembic ; aucune coroutine
  non attendue. Les skips restent explicitement conditionnels.
- Ruff : `All checks passed!`.
- mypy : aucune erreur dans 171 fichiers.
- `git diff --check` propre ; index vide ; aucun commit.
- Journaux locaux : `/tmp/botradar-freshness-verification/full-delivery-final.log`.

La dernière régression a aussi confirmé puis corrigé la conservation de l'agrégat
brut à la première collecte après écriture de la tentative. Cela préserve les
informations historiques sans les convertir en preuves annuelles.
Les tests conditionnels PostgreSQL et certaines caractérisations ne sont pas
une preuve locale PostgreSQL ; aucune CI n'a été lancée sans push.
La revue indépendante statique a relevé puis fait corriger les défauts de
couverture, de fraîcheur et de cotations hors vagues ; elle ne constituait pas la
revue humaine alors requise.

Sans publication explicite, la frontière est le premier catalogue réussi : les
nouveautés entre confirmation et premier passage peuvent manquer, et une ancienne
annonce inconnue découverte plus tard n'est pas datable. Une panne fournisseur
persistante peut empêcher une réussite en 48 h : le code signale le dépassement
sans transformer la tentative en succès. Les preuves Grimr manquent encore ;
aucune valeur 5,45 € n'est affichée comme certifiée sans elles.

À la fin de cette vérification initiale restaient : revue humaine, livraison du code, migration puis
vérification réelle du scheduler, des collectes et des notifications. Aucun de
ces résultats locaux n'atteste un déploiement ni une réussite future des sources.
