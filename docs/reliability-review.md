# Rafraîchissements, /add et cotation — revue du 16 octobre 2026

Verdict historique de la seconde auto-revue : **prêt pour revue**. Aucun échec nouveau dans
la suite isolée ; trois échecs préexistants demeurent. Preuves historiques en section 7.
Aucun commit, push, PR, merge ou déploiement effectué. Aucune intervention de
production, migration, connexion PostgreSQL ou Redis réelle.

- Branche : `fix/reliable-refresh-add`.
- Worktree : `/Users/aminezegmou/Projets/bot-radar-worktrees/reliable-refresh-add`.
- Base et HEAD : `5cbba4c9520459ce69decebbd2bf1be2b6cc950a` ; modifications non committées.
- `main` local et distant ont été vérifiés à cette base pendant l'investigation.
  Le dossier principal et les autres travaux ont été conservés.
- PR #121 : `68643183f6e8b85fc8fccfdc19144aef8d1e5981`, fusion à 09:29:08 UTC.
- PR #122 : `e8caa9c58a63bce11b216033140f816c4049b27f`, fusion à 10:58:29 UTC.
  Ces fusions ne prouvent pas une image déployée.

Date et résultats du mandat de préparation : voir la [feuille de route](reliability-roadmap.md).
Cette page conserve les preuves des revues précédentes.

L'auto-revue ciblée suivante a identifié deux défauts résiduels. Leur correction
locale est consignée en section 10 ; son contrôle final reste une auto-revue,
sans validation humaine, PostgreSQL ou CI.

## 1. Causes prouvées dans le code de base

| Chemin et fonctions | Fait établi |
| --- | --- |
| `app/tasks/refresh_book_prices.py` : `refresh_book_prices_task`, `_report_stale_certs` ; `app/services/market_health.py` : fonctions `notify_*` | Les contrôles stale/historique/silence précédaient les trois notifications finales. Leurs créneaux étaient indépendants : le dédoublonnage du bilan ne coordonnait pas les autres messages. Le seuil de sept jours subsistait après #121. |
| `app/services/market_health.py` : `notify_high_error_ratio` | Le compteur `degraded` pouvait produire « livres en erreur » alors qu'aucune exception de traitement n'avait été comptée. Le texte ne permettait donc pas d'attribuer la cause du « 23/23 ». |
| `app/tasks/refresh_book_prices.py` : `_cote_was_written` | Le test du seul état `complete` excluait les offres courantes recueillies lors d'une collecte partielle. Un cache seul ne prouve pas davantage une nouvelle observation de cette passe. |
| `app/providers/{momox,gibert,recyclivre}.py` : `get_offers` ; `app/providers/chasse_aux_livres.py` : recherches et extraction | Plusieurs échecs HTTP et exceptions étaient réduits aux noms de sources. L'historique pouvait dégrader le résultat de la même source sans préciser que la collecte courante avait réussi. |
| `app/services/book_metadata_service.py` : `parse_query_title_and_author` | Le découpage au dernier « de » transformait « Le jour de sable » en titre « Le jour » et auteur « sable », et tronquait « Aimée de Jongh ». Les sondes puis régressions l'ont reproduit. |
| `app/bot/handlers/alerts_helpers.py` : `extract_inline_price_and_clean_query`, `realign_draft_to_isbn` | La virgule séparant le prix restait dans la requête. Le réalignement transmettait le nom artificiel comme titre attendu et conservait ce nom après confirmation. |
| `app/services/ai/modules/natural_add.py` : `parse_prompt`, `_resolve_isbn` | La notice confirmée ne remplaçait pas systématiquement le titre interprété. Un ISBN proposé par l'IA pouvait être soumis comme candidat. Ce chemin a été retiré. |
| `app/domain/rules/keyword_hygiene.py` : `assess_exclusions`, `sanitize_exclusions` ; `app/bot/handlers/alerts_create.py` : recherches de mots-clés et `_save_alert` | Le contrôle final ne recevait que le titre. Les exclusions initiales de `NaturalAddModule.parse_prompt` étaient fusionnées avec celles de `KeywordVariantsModule`, puis validées sans autrice ni éditeur. `aimée de jongh` et `dargaud` survivaient. L'origine exacte de chacune des valeurs de la carte observée reste inconnue sans traces. |
| `app/bot/handlers/alerts_helpers.py` : réservation et application des recherches ; handlers de création | Le compteur de recherche repartait après vidage du FSM. Une annulation pendant le message de chargement précédait même la réservation du jeton : une réponse pouvait recréer le brouillon. Ces deux courses ont été reproduites. |
| `app/use_cases/prepare_bd_alert.py` : `PreparationRunner._run` | Un cache non nul était un résultat technique, sans garantir un montant exploitable. Ce fait ne prouve pas un défaut fournisseur. |

## 2. Limites et hypothèses restantes

Aucun log de production exploitable, horaire complet de la passe ou identifiant
vérifié de l'image exécutée n'a été fourni. Pour **chasse_aux_livres, gibert, momox
et recyclivre**, la cause réelle du « 23/23 » reste **indéterminée**. Timeout,
limitation, statut HTTP, parsing et erreur interne sont des catégories
instrumentées, pas des causes de production démontrées. La configuration réelle
et la validité des sessions restent à vérifier séparément, sans lecture des secrets ici.

Une recherche vide valide n'est pas une panne. Le silence des snapshots ne prouve
pas l'indisponibilité d'un site. Une défaillance globale de collecte ne permet pas
de désigner indistinctement tous les fournisseurs.

La [fiche officielle Dargaud](https://www.dargaud.com/bd/jours-de-sable-bda5322000)
corrobore « Jours de sable », Aimée de Jongh et l'ISBN 9782505082545. Les montants
manuels de 20,59 € et 8,99 € n'ont pas été corroborés avec édition, état, livraison,
date et période comparables. Aucune collecte réelle ni réparation fournisseur
n'est attestée. Les montants des tests sont des fixtures, jamais des constantes métier.

## 3. Modifications apportées

- `CollectionAttempt`, résultats par livre et `RefreshPassResult` séparent les
  observations de cette tentative, le cache réutilisé, les absences normales,
  les dégradations et les exceptions. Les dates de tentative, de succès courant
  et d'historique restent distinctes.
- Un seul orchestrateur produit le bilan complet, même sur sélection vide ou
  interruption technique, puis laisse remonter l'exception. Les listes stale
  détaillées restent disponibles hors du flux automatique ; leurs comptes
  figurent dans la synthèse et les journaux.
- Les incidents sont suivis par source/opération, séparément du traitement et
  de la passe. État inchangé : silence incrémental. Aggravation : nouveau
  composant, perte totale après résultat partiel, ou hausse de 25 points sur
  au moins trois tentatives de part et d'autre. Une source non interrogée
  n'est pas déclarée rétablie.
- Redis conserve cet état, avec repli local. Le bilan garde son créneau d'une
  heure par occurrence ; l'échec de remise libère ce créneau. Les diagnostics
  portent catégorie, statut HTTP, opération, durée, livre et occurrence.
- L'ISBN saisi se résout avant l'IA. Saisie originale, requêtes et titre canonique
  sont séparés. Une notice peut identifier un brouillon non identifié ; une
  identité établie exige confirmation explicite du remplacement. Le choix est
  lié à une proposition et revalidé. Les critères dérivés sont réalignés ensemble.
- Les choix d'œuvres restent courts et une faute de titre ne permet pas au seul
  classement lexical de trancher entre des œuvres distinctes. Les numéros de
  tome explicitement demandés restent conservés.
- `/skip` retire l'ISBN et le conflit en attente, puis rend le récap actualisé,
  sans transformer un one-shot en série. Jeton, révision et `draft_guard`
  protègent l'application des réponses ; le réseau reste hors de cette garde.
- Les exclusions initiales et générées sont contrôlées après fusion avec les
  métadonnées confirmées, puis avant persistance. Le matching d'une annonce
  mentionnant correctement l'autrice et l'éditeur est testé.
- La préparation initiale est bornée à 45 secondes, attente de créneau comprise.
  Le cache valable est réutilisé. Une seule reprise initiale des sources
  courantes en échec transitoire est permise ; les sources réussies ne sont
  pas relancées. Un `Retry-After` trop long est différé. Parsing et configuration
  ne provoquent pas cette reprise. Les erreurs de persistance restent bloquantes.
- Le prix courant peut s'afficher sans historique. Les cartes distinguent prix
  actuel et prix conservé, collecte vide et échec. L'actualisation utilise la même
  carte. Le prix bas indique les relevés et leur période ; il ne prétend pas
  représenter un minimum annuel exhaustif.

Aucune formule de marché, règle de comparabilité, concurrence, migration ou
architecture de scheduler n'a été ajoutée. Le chemin ingestion → cache → Telegram
reste préservé ; les cotations continuent de passer par `book_market_pricing`.
Les contrats catalogue/marché, Telegram/IA et préparation ont été actualisés.

## 4. Tests réellement exécutés

Preuves antérieures à la seconde revue : contrôles exécutés séquentiellement dans le worktree
sur le diff alors présent. Les résultats actualisés figurent en section 7 :

```sh
/Users/aminezegmou/Projets/bot-radar/.venv/bin/ruff check .
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py typecheck
make test-isolated PYTHON=/Users/aminezegmou/Projets/bot-radar/.venv/bin/python
```

- Ruff : **All checks passed!**
- Typecheck isolé : **aucune erreur sur 162 fichiers**, puis **aucune erreur sur 4 fichiers** d'outillage. Ce mode appelle mypy sur `app` et `scripts/matching_diagnostics.py`, puis l'outillage prévu par la recette.
- Suite complète : **3 failed, 2080 passed, 12 skipped, 4 warnings in 75.06s (0:01:15)**. Pytest sort avec **1**, `make` avec **2**. Les trois échecs sont ceux reproduits sur main, détaillés ci-dessous.
- **44 scénarios de régression ajoutés : tous réussis**, inclus dans la suite complète.
- Dernier contrôle ciblé du rendu : **87 réussites en 0,72 s**.
- Les quatre avertissements concernent des dépréciations Starlette/FastAPI et la configuration Alembic ; aucun test n'a été désactivé pour les masquer.
- `git diff --check` : propre ; index vide. Auto-revue du diff et des nouveaux tests effectuée.

Les sorties complètes locales sont conservées dans `/tmp/radar-final-ruff.txt`,
`/tmp/radar-final-mypy.txt`, `/tmp/radar-final-tests.txt` et la reproduction sur main
`/tmp/radar-main-scheduler.txt`. Ces chemins sont temporaires ; le présent rapport
consigne les résultats et leur périmètre.

Les régressions couvrent les cinq commandes `/add` avec IA activée/désactivée,
le placeholder, le remplacement confirmé, `/skip`, les exclusions fusionnées,
le matching, le cache valable, le prix sans historique, les dates conservées,
les courses d'annulation, les incidents, leur aggravation et leur rétablissement,
Redis indisponible, l'échec d'envoi, la sélection vide et les interruptions.
Les phases rouges ont reproduit les défauts avant correction. Les régressions
intermédiaires (tome perdu, doubles FSM incomplets, constructeur de registre
simulé et ancien contrôle d'ISBN IA) ont été corrigées et leurs consommateurs rejoués.

Les contrôles utilisent l'interpréteur existant et importent le code du worktree.
Le lanceur nettoie la configuration avant imports, neutralise les dotenv/secrets
et refuse les transports réels, enfants compris. Les tests SQL locaux utilisent
SQLite ; ils **ne prouvent pas** les verrous ou la concurrence PostgreSQL.
Aucun résultat CI ni résultat de production n'est revendiqué.

## 5. Échecs préexistants séparés

Le même lanceur, sur `main` propre à `5cbba4c`, reproduit les trois échecs de :

- `tests/integration/test_scheduler_occurrence.py::test_the_real_scheduler_hands_the_intended_run_time_to_the_job`
- `tests/integration/test_scheduler_occurrence.py::test_the_transmitted_time_is_the_triggered_run_not_the_next_one`
- `tests/integration/test_scheduler_occurrence.py::test_a_delayed_run_is_still_identified_by_its_own_occurrence`

Commande de reproduction :

```sh
.venv/bin/python -B scripts/verify_consolidation.py unit --target tests/integration/test_scheduler_occurrence.py
```

Résultat sur main : **3 échecs en 0,16 s**. Le garde du lanceur refuse le démarrage
avec « Scheduler réel interdit » ; le nettoyage lève ensuite `SchedulerNotRunningError`.
La recette n'a pas été contournée et les tests n'ont pas été désactivés. Cette
limite de vérification reste à traiter avant de déclarer la branche entièrement verte.

## 6. Risques résiduels et vérification après déploiement

- Exécuter les contrôles PostgreSQL concernés sur une base jetable uniquement
  après autorisation distincte. Les tests et leurs assertions sont conservés.
- La remise Telegram n'est pas transactionnelle avec Redis : un acquittement
  ambigu ou une remise partielle à plusieurs destinataires peut entraîner une
  répétition. Le repli local ne survit pas au redémarrage et le créneau expire.
- Une cotation partagée engagée peut finir dans sa session après interruption de
  la préparation ; elle peut enrichir le cache du livre, sans réactiver ni
  réécrire l'identité d'une demande remplacée. Un dépassement connu après lecture
  de la demande laisse une préparation bloquée, à relancer explicitement.
- Aucun historique existant ni alerte existante n'a été nettoyé en masse. Les
  anciens prix et observations restent conservés, avec leur date réelle.
- La revue du diff et de l'index est une **auto-revue**, pas une revue indépendante.

Après un déploiement séparément autorisé : vérifier l'image réellement exécutée
et son SHA, la santé de l'app et le scheduler unique ; observer une occurrence
complète et compter ses messages, sans liste stale séparée ; comparer les compteurs
aux diagnostics de tentative. Observer ensuite un incident et son rétablissement
avec source/opération/statut HTTP, sans déduire une panne du seul nom du site.
Vérifier le parcours `/add` et sa carte, puis l'ISBN, l'état, la livraison et les dates
des observations pour tout prix comparé. Ces opérations ne sont pas exécutées ici.

Au terme de la première revue, le diff non committé restait à l’état **partiel**.
La seconde revue ci-dessous actualise ce verdict sans attester une validation
PostgreSQL ni donner d’autorisation implicite de merge.


## 7. Seconde auto-revue du diff non committé

Le diff applicatif, ses consommateurs, les changements de tests, les trois nouveaux
fichiers et les contrats modifiés ont été relus. Aucun changement du scheduler,
des formules de marché, des migrations ou du chemin ingestion/cache/Telegram
n'a été ajouté. Les corrections suivantes concernent directement le mandat.

| Localisation | Défaut reproduit et correction |
| --- | --- |
| `alerts_helpers.render_existing_alert_response`, `alerts_create.start_add_alert`, `alerts_helpers.process_bd_query_lookup` | Une réponse DB anti-doublon tardive ajoutait `existing_alert_id` et changeait l'état du nouveau brouillon. L'application vérifie maintenant le jeton attendu sous `draft_guard`, avant mutation ; le réseau reste hors garde. |
| `alerts_create.start_add_alert` | Une réponse IA vide après annulation rétablissait la sélection de catégorie. Le repli vérifie maintenant le jeton sous garde, comme le chemin identifié. Le repli sans IA est également gardé après la recherche DB. |
| `market_price_service._current_collection_is_empty` | `partial` et une liste d'offres vide ne prouvaient pas une recherche courante réussie : un résultat historique seul pouvait afficher « aucune offre trouvée » malgré l'échec courant. Le rendu exige désormais la date de succès de cette tentative. |
| `ProviderRegistry.fetch_all_offers` | Une source annulée figurait dans `failed_providers` mais pas dans les tentatives dès qu'une autre source répondait. Elle conserve désormais une tentative échouée `cancelled`, donc un diagnostic visible par source. L'annulation de l'appel global continue de remonter. |
| `ChasseAuxLivresProvider._search_query` | Un JSON sans champ `d` était pris pour une recherche vide réussie. Le schéma est maintenant validé ; une réponse malformée porte `invalid_response`. Aucun rétablissement réel du fournisseur n'est attesté. |
| `market_health.notify_refresh_result` | Une annulation pendant l'envoi gardait le créneau de bilan acquis. Elle le libère maintenant sans acquitter l'incident et remonte au demandeur. La remise externe reste non transactionnelle. |
| `refresh_book_prices._book_result`, `_run_wave`, `market_health.notify_refresh_result` | La réutilisation d'un cache n'était pas visible dans les compteurs. Le bilan affiche maintenant `Caches réutilisés`, sans incrémenter les actualisations ni créer de tentative fournisseur. |

Huit scénarios ont été ajoutés dans `tests/unit/test_reliability_review.py`.
Leur phase rouge a reproduit les défauts ; ils passent après correction. Le test de
reprise sélective existant interroge désormais aussi une source saine et vérifie
qu'elle n'est appelée qu'une fois. La preuve antérieure ne testait qu'une source
échouée puis rétablie et ne suffisait pas à justifier son nom.

Contrôles intermédiaires : 165 réussites en 0,82 s ; 72 réussites en 0,65 s ;
36 réussites en 0,50 s. Deux lignes trop longues puis une annotation `update_data`
ont été corrigées ; aucun de ces échecs intermédiaires n'a été masqué.

La borne de 45 secondes porte sur le bloc initial de préparation, attente du
créneau comprise. L'enregistrement de son échec s'effectue ensuite : la durée
murale totale de cette écriture DB n'est pas prouvée ici. Les limites de remise
Telegram, de dédoublonnage borné et de persistance après interruption décrites
plus haut restent applicables. Une source observée sur un seul livre ne prouve
pas son rétablissement sur tous les livres ou toutes les éditions.


### Résultats finaux de la seconde revue

Les commandes suivantes ont été exécutées séquentiellement dans le worktree,
avec l'interpréteur du dépôt principal et les imports du worktree :

```sh
/Users/aminezegmou/Projets/bot-radar/.venv/bin/ruff check .
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py typecheck
make test-isolated PYTHON=/Users/aminezegmou/Projets/bot-radar/.venv/bin/python
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py unit --target tests/integration/test_scheduler_occurrence.py
```

| Contrôle | Résultat final |
| --- | --- |
| Ruff complet | `All checks passed!`, sortie 0 |
| Typecheck isolé | Aucune erreur sur 162 fichiers, puis 4 fichiers, sortie 0 |
| Suite isolée complète | **3 failed, 2088 passed, 12 skipped, 4 warnings in 70.29s**, pytest 1 / make 2 |
| Reproduction scheduler séparée | **3 failed in 0.14s**, sortie 1 |

Par rapport au point de départ : huit réussites supplémentaires, aucun échec
nouveau, mêmes douze tests ignorés. Les huit scénarios de cette revue passent ;
les 44 scénarios ajoutés précédemment restent verts. Les trois identifiants
scheduler et leurs causes sont identiques à la preuve main conservée en section 5 :
`Scheduler réel interdit`, puis `SchedulerNotRunningError` au nettoyage.
Le scheduler et le lanceur n'ont pas été modifiés.

Une exécution intermédiaire complète avait donné 5 failed, 2086 passed,
12 skipped, 6 warnings en 71,98 s. Les deux échecs nouveaux provenaient des doubles
FSM de `test_multi_category_creation` qui ne mémorisaient pas le jeton écrit.
Ils utilisent maintenant un vrai FSM mémoire ; leurs assertions sont conservées
et renforcées par la lecture de l'état final. Le test sans argument vérifie
exactement l'effacement puis le passage à la sélection de catégorie.
La sélection concernée donne **29 réussites en 0,17 s**. Aucun test n'a été
supprimé, désactivé ou affaibli. Les deux avertissements de coroutine non attendue
de cette exécution intermédiaire ont disparu avec ces doubles corrigés.

Sorties actualisées : `/tmp/radar-review-ruff.txt`, `/tmp/radar-review-mypy.txt`,
`/tmp/radar-review-tests-final.txt`, `/tmp/radar-review-scheduler.txt`.
La recette finale confirme `allowed: 0` dans le parent et les enfants : aucun
transport réel autorisé. La phase d'auto-vérification du lanceur refuse elle-même
ses sondes réseau et dotenv ; ces refus ne sont pas des accès aux services.

`git diff --check` est propre, l'index reste vide et HEAD reste `5cbba4c`.
Le dossier principal est propre. Aucun commit, push, PR, merge, déploiement,
migration, accès réel PostgreSQL/Redis ou lecture de secrets n'a été effectué.

Les durées de diagnostic des tentatives mesurent l'appel connecteur complet,
pas chacune de ses requêtes HTTP. Les tests SQL restent des preuves SQLite.
La cause de production du « 23/23 », l'image exécutée et les prix manuels restent
non vérifiés. Cette auto-revue ne constitue pas une revue indépendante.

Prochaines étapes minimales : relire le diff et ce rapport ; traiter la limite du
lanceur scheduler dans un mandat distinct ; obtenir séparément l'autorisation
et les ressources pour les preuves PostgreSQL nécessaires. Le correctif reste
non committé, prêt pour revue, sans mandat Git de livraison ni déploiement.


## 8. Préparation autonome de la revue formelle

La [feuille de route du mandat suivant](reliability-roadmap.md) distingue les
étapes locales terminées, les décisions H1/H2/H3, les mandats distincts et les
vérifications après déploiement. Elle contient les recettes PostgreSQL non
exécutées et les projets de message de commit et de description de PR.

Ce mandat a corrigé les critères périmés affichés après recherche de variantes,
le texte trop général des résultats partiels, l'état persistant des sources non
interrogées et la conservation des anciens prix informatifs dans ce cas. Il a
également corrigé la fausse couverture du mode sans IA dans le double de parcours
et isolé les tâches de fond de deux tests de panneau, avec attente explicite.

Résultats actualisés : Ruff et typecheck verts ; **3 failed, 2095 passed,
12 skipped, 4 warnings en 77,86 s** ; les trois tests scheduler reproduits seuls
en **0,15 s**, mêmes causes que la preuve main. **59 régressions ajoutées vertes**.
Aucun échec nouveau, service réel ou opération Git de livraison. Le verdict du
mandat autonome est **autonome terminé** ; les preuves PostgreSQL, CI et de
production et la revue indépendante restent non acquises.

## 9. Reprise locale des trois constats de l'examen distinct

Mandat du 16 octobre 2026 : corrections locales limitées aux trois constats,
tests et documentation associés. H1–H3 restent acquis, sans publication ni service
réel. Les résultats des sections précédentes sont des preuves historiques.
Le nouvel examen de ces corrections par le relecteur reste nécessaire ; la présente
vérification de l'implémentation est une auto-vérification.

### Causes et corrections

1. `CreationDraftMiddleware._dispatch` supprimait l'identité du callback avant
   l'appel à `handle_category_selected`. Après `callback.answer()`, le handler
   lisait le nouveau brouillon sans revalider l'interaction. Le middleware conserve
   désormais le callback original ; le handler le capture avant acquittement puis
   le revalide sous `draft_guard`. Les recherches appelées portent aussi leur jeton
   jusqu'à l'application ; le parcours hors livres utilise la même réservation et
   la même application gardées, y compris après recherche de doublon. Aucun réseau
   n'est ajouté sous cette garde. Deux anciens tests appelant directement le handler
   transmettent maintenant le véritable format de callback lié au brouillon.
2. `_incident_changes` conservait le taux notifié lors d'une amélioration partielle.
   L'orchestrateur distingue maintenant dernière observation, état notifié et
   changements en attente. L'observation silencieuse est persistée ; un envoi échoué
   conserve le changement à remettre. La même clé Redis contient ces trois vues,
   avec lecture de l'ancien format et repli local inchangés. Un `skipped`, une source
   absente ou la réussite de sa seule autre opération ne vaut pas rétablissement.
3. `_save_alert` perdait le mode bibliographique avant préparation. Le JSON durable
   porte désormais `bibliographic_mode`, conservé par `restart` et `suspend`.
   `PreparationRunner._run` sélectionne la recherche unitaire pour `one_shot`.
   Mode absent ou NULL : inconnu, chemin série historique conservé, sans
   requalification. Les demandes connues série restent séries. Aucun changement
   de colonne ou migration ; la garde de version avant rattachement et activation
   est conservée.

### Preuves avant/après

La première sélection du lanceur isolé a donné **5 failed, 1 passed en 0,30 s** :
mutation des jetons/catégorie de B par A, aggravation silencieuse avec Redis simulé
ou indisponible, et absence de rattachement après `/skip` en création ou en reprise.
Le contrôle d'échec du premier envoi puis nouvelle tentative passait déjà.
Sortie : `/tmp/radar-followup-red.txt`.

Après correction, **66 passed en 1,83 s**, puis **121 passed en 3,06 s** après ajout
des chemins appelés et de la compatibilité durable. La sélection complète des
consommateurs donne **156 passed en 2,53 s**, sans avertissement.
Les quatorze nouveaux scénarios couvrent :

- le vrai middleware, l'acquittement suspendu de A et B intégralement inchangé ;
- les recherches livres/hors livres suspendues puis remplacées, garde libre pendant
  l'attente et aucun résultat ancien appliqué ;
- `3/3 → 1/3 → 3/3`, relecture JSON entre passes, repli local, envoi refusé puis
  réessayé, silence inchangé et rétablissement sur observation ;
- lecture du format Redis historique, absence de faux rétablissement sur `skipped`
  ou réussite historique seule ;
- `/skip → confirmation → persistance → notice unitaire non numérotée → liaison →
  prix courant`, en parcours immédiat et reprise dans un nouveau runner sans FSM ;
- relance durable de chacun des modes connu unitaire, connu série, NULL ou absent ;
- ancien résultat unitaire incapable de lier, coter ou activer après remplacement.

Deux erreurs de câblage des tests supplémentaires ont été corrigées : écriture du
mode non commitée avant une relecture du dépôt, et double de méthode sans argument
`self`. L'exécution intermédiaire donnait 3 failed, 118 passed en 5,30 s ; ce ne sont
pas des régressions applicatives. L'invocation initiale avec `-k` a été refusée par
le lanceur ; les sélections suivantes utilisent ses `--target` existants.

Les doubles ne remplacent ni la persistance, ni le rattachement, ni le calcul et
l'écriture du cache dans le parcours `/skip` ; seules les frontières catalogue et
collecte sont simulées. Le backend exécuté est SQLite, sans preuve PostgreSQL.
Redis et Telegram sont simulés ; aucune validation réelle de ces services.

### Contrôles finaux de cette reprise

Exécutés depuis le worktree, après le dernier changement applicatif et dans cet
ordre, avec `/usr/bin/time -p` :

```sh
RUFF_CACHE_DIR=/tmp/radar-followup-ruff-cache /Users/aminezegmou/Projets/bot-radar/.venv/bin/ruff check .
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py typecheck
make test-isolated PYTHON=/Users/aminezegmou/Projets/bot-radar/.venv/bin/python
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py unit --target tests/integration/test_scheduler_occurrence.py
```

| Contrôle | Résultat exact | Durée |
| --- | --- | --- |
| Ruff complet | `All checks passed!`, sortie 0 | 0,06 s murales |
| Typecheck isolé | Aucune erreur sur 162 puis 4 fichiers, sortie 0 | 17,51 s murales |
| Suite isolée complète | **3 failed, 2109 passed, 12 skipped, 4 warnings**, pytest 1 / make 2 | 91,43 s pytest ; 95,78 s murales |
| Scheduler seul | **3 failed**, sortie 1 | 0,20 s pytest ; 3,45 s murales |

Comparaison avec l'arbre de préparation précédent : **14 réussites supplémentaires**
par rapport à 2095, mêmes 3 échecs, mêmes 12 ignorés et 4 avertissements. Les
quatorze nouveaux scénarios ont tous un rapport `call: passed` ; 2124 tests sont
collectés au total. Les trois nodeids scheduler et leurs causes sont inchangés :
`Scheduler réel interdit`, puis `SchedulerNotRunningError` au nettoyage. La preuve
sur main reste celle historique de cette même base ; aucun rejeu ou changement
d'un autre worktree n'a été effectué. Le garde, le scheduler et ses tests restent
inchangés dans ce lot.

Ruff a d'abord refusé son cache dans le worktree ; il a été dirigé vers `/tmp`.
Une première tentative typecheck s'est arrêtée avant analyse avec
`sqlite3.OperationalError: unable to open database file` sur le cache mypy.
Le même lanceur a ensuite été rejoué avec permission d'écrire ce cache dans le
worktree, sans modification du lanceur ni de ses gardes.

Sorties : `/tmp/radar-followup-{ruff,typecheck,tests,scheduler}.txt` et
`/tmp/radar-followup-targeted.txt`. Les rapports d'isolation indiquent `allowed: 0`
dans le parent et les enfants ; les trois démarrages scheduler interdits sont
refusés. Aucun secret ou service réel utilisé.

État : **corrections locales et vérifications terminées, à faire réexaminer**.
HEAD reste `5cbba4c9520459ce69decebbd2bf1be2b6cc950a`, branche
`fix/reliable-refresh-add`, diff non committé, index vide et `git diff --check`
propre. Les travaux présents sont conservés. Aucune publication, migration,
opération PostgreSQL/Redis réelle, modification de main ou d'un autre worktree.

La recette PostgreSQL conserve sa commande et ses ressources : sa cible existante
`test_bd_preparation.py` inclut les sept nouvelles régressions d'intégration.
Elle reste non exécutée. À réexaminer : identité du callback avant attente et aux
mutations appelées ; états observé/notifié/en attente avec remise échouée ; mode
durable, compatibilité historique et garde de version ; délais de persistance et
récupération exigés par H2. La concurrence PostgreSQL, la CI, les fournisseurs et
la production restent sans validation nouvelle. Cette auto-vérification ne clôt
pas les constats à la place du relecteur.

## 10. Correction des deux défauts résiduels après auto-revue ciblée

Mandat local limité aux interactions de catégorie du même brouillon et à la remise
d'un incident rétabli avant sa première notification. Les conclusions antérieures
restent historiques ; ce contrôle n'est pas une revue indépendante.

### Causes, corrections et tests

- `handle_category_selected` ne contrôlait que l'identité de création après
  acquittement. Il relève maintenant la génération et vérifie la phase avant
  acquittement, sans modifier le brouillon ; après l'attente, identité originale,
  génération et phase sont revalidées sous `draft_guard` avant mutation. Le réseau
  reste hors garde. Le nouveau test passe par le vrai middleware, suspend le
  premier clic, applique le second, saisit un ISBN jusqu'au récapitulatif puis
  compare intégralement données, jetons et FSM après reprise du premier clic.
  Il vérifie aussi le fonctionnement du choix valide et l'absence d'affichage
  obsolète. Les doubles existants explicitent leur phase FSM ; aucun test n'est
  désactivé ou affaibli.
- `notify_refresh_result` effaçait le changement en attente lors d'un rétablissement
  jamais notifié. Il conserve désormais « Incident observé puis rétabli avant
  remise », séparément de l'observation récente et de l'état notifié. Le même JSON
  persiste cette synthèse, sans nouvelle infrastructure. Un échec conserve la
  synthèse ; une remise confirmée la retire. Une nouvelle panne avant remise
  conserve la synthèse passée avec un nouvel incident explicitement en cours.
  Quatre scénarios combinent Redis simulé disponible/indisponible et remise de la
  synthèse réussie/échouée ; ils vérifient les trois vues, la relecture entre étapes,
  l'absence de faux rétablissement sur `skipped`/absence/historique seul et l'absence
  de doublon après confirmation. Un cinquième couvre la nouvelle panne avant remise.

### Preuves avant/après ciblées

Avant correction, le lanceur isolé a exécuté les deux nouvelles fonctions avec
leurs paramètres : **5 failed en 0,19 s**. Le clic ancien changeait catégorie et
jeton malgré le récapitulatif ; les quatre cas d'incident perdaient la synthèse.
Sortie : `/tmp/radar-residual-red.txt`.

Après correction et ajout du scénario de nouvelle panne : **112 passed en 0,49 s**
sur les consommateurs création/notifications/contrat Telegram. Les transitions
antérieures, dont `3/3 → 1/3 → 3/3`, restent exercées. Une première exécution
intermédiaire donnait 1 failed, 110 passed en 0,56 s : un import manquait dans un
test adapté à la phase FSM ; il a été corrigé. Ruff ciblé a signalé une assertion
composée, ensuite séparée. Sorties : `/tmp/radar-residual-green{1,2}.txt`.

Les seules modifications applicatives de cette reprise concernent
`alerts_create.handle_category_selected` et `market_health.notify_refresh_result`.
One-shot après `/skip` : clos sur examen statique, preuve PostgreSQL toujours
manquante. H1 reste séparé ; H2 ne promet aucune durée totale. Aucun nouveau défaut
d'activation indue confirmé ; fournisseurs, CI et production restent non validés.

### Contrôles finaux et état de cette reprise

Après le dernier changement applicatif, commandes exécutées dans cet ordre depuis
le worktree, avec `/usr/bin/time -p` :

```sh
RUFF_CACHE_DIR=/tmp/radar-residual-ruff-cache /Users/aminezegmou/Projets/bot-radar/.venv/bin/ruff check .
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py typecheck
make test-isolated PYTHON=/Users/aminezegmou/Projets/bot-radar/.venv/bin/python
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py unit --target tests/integration/test_scheduler_occurrence.py
```

| Contrôle | Résultat exact | Durée |
| --- | --- | --- |
| Ruff complet | `All checks passed!`, sortie 0 | 0,08 s murales |
| Typecheck isolé | Aucune erreur sur 162 puis 4 fichiers, sortie 0 | 16,57 s murales |
| Suite isolée complète | **3 failed, 2115 passed, 12 skipped, 4 warnings**, pytest 1 / make 2 | 74,24 s pytest ; 77,60 s murales |
| Scheduler ciblé | **3 failed**, sortie 1 | 0,16 s pytest ; 2,59 s murales |

Comparaison avec la reprise précédente : 6 réussites supplémentaires par rapport
à 2109, mêmes 3 échecs, mêmes 12 ignorés et 4 avertissements. Les six scénarios
ajoutés passent dans la suite complète ; 2130 scénarios sont collectés. Les trois
échecs scheduler restent :

- `test_the_real_scheduler_hands_the_intended_run_time_to_the_job` ;
- `test_the_transmitted_time_is_the_triggered_run_not_the_next_one` ;
- `test_a_delayed_run_is_still_identified_by_its_own_occurrence`.

Même cause et même signature : `Scheduler réel interdit`, puis
`SchedulerNotRunningError` au nettoyage. Scheduler, tests scheduler et garde
inchangés ; aucune désactivation ni contournement. La preuve main reste historique.

Sorties : `/tmp/radar-residual-{ruff,typecheck,tests,scheduler}.txt`. Ruff utilise
un cache dans `/tmp` pour respecter la sandbox ; le typecheck utilise l'autorisation
d'écriture locale de son cache mypy, sans changer le lanceur. Les rapports
d'isolation attestent `allowed: 0` dans parent et enfants et trois démarrages
scheduler refusés. Aucun secret ou service réel utilisé.

Branche `fix/reliable-refresh-add`, HEAD inchangé à
`5cbba4c9520459ce69decebbd2bf1be2b6cc950a`, diff non committé et index vide ;
`git diff --check` propre. Aucune opération Git de livraison, migration ou action
sur un autre worktree. Les preuves historiques sont conservées.

État historique de cette reprise : correction locale et contrôles terminés ;
**auto-revue ciblée uniquement**. Le réexamen distinct ultérieur et le complément
de couverture sont consignés en section 11 ; cette preuve historique ne les remplace
pas, et ne valide ni PostgreSQL ni la CI.

## 11. Réexamen distinct clos et complément de couverture — 16 octobre 2026

### Conclusions distinctes et portée du complément

Le relecteur n'avait pas participé à l'implémentation des deux corrections.
Son examen du diff local et des tests, en lecture seule et sans exécution, conclut :

- Ancien callback du même brouillon : **clos sur examen statique** ; identité originale,
  phase et génération sont revalidées avant mutation après l'acquittement.
- Incident non remis puis rétabli : **clos sur examen statique** ; observation,
  état notifié et synthèse en attente restent séparés. Aucun défaut applicatif
  résiduel n'est confirmé dans ce périmètre.

La couverture manquante concernait la combinaison nouvelle panne et relecture
persistée : le test antérieur de nouvelle panne conservait ses états locaux.
Le mandat suivant autorise uniquement ce complément et les contrôles locaux.
Il ne rouvre pas les deux constats ni une revue générale.

### Preuve supplémentaire exécutée par l'auteur du test

Le test ajouté est
[`test_new_failure_and_past_recovery_survive_persisted_restarts`](../tests/unit/test_reliability_followup.py).
Le stockage simulé ne conserve que les chaînes JSON effectivement écrites et les
restitue à la lecture. Le scénario vérifie successivement : incident non remis,
rétablissement avec synthèse persistée, reprise et nouvelle panne non remise,
nouvelle reprise avec remise réussie des deux événements, puis passage identique
silencieux après une autre reprise.

Chaque reprise remplace les trois dictionnaires locaux, l'indicateur de chargement,
le verrou, les fenêtres locales et les doubles Redis/Telegram. Les résultats de
passe sont recréés. Les assertions comparent le JSON relu aux trois vues locales
et aux états attendus, contrôlent le texte passé/courant, les lectures Redis,
la vidange de l'attente après remise et l'absence d'envoi suivant. Aucun objet
métier conservé avant reprise ne fournit l'état attendu au code exécuté.

Le premier lancement passe : **1 passed en 0,10 s**, sortie 0, 2,53 s murales.
Aucune phase rouge ni modification applicative n'a été nécessaire. Il s'agit de
**couverture supplémentaire**, exécutée par l'auteur de ce complément ; le test
ajouté n'a pas lui-même fait l'objet d'une revue distincte.

Ruff a ensuite signalé une ligne de 105 caractères dans ce seul test ; elle a été
coupée, sans changement de comportement. Le test ciblé final repasse : **1 passed
en 0,10 s**, sortie 0, 2,68 s murales. Le premier typecheck s'est arrêté avant analyse
sur `sqlite3.OperationalError: unable to open database file` (cache mypy inaccessible
sous la sandbox, sortie 1, 0,34 s). La même commande isolée a été reprise avec
l'autorisation d'écriture locale de ce cache, sans modifier le lanceur.

### Contrôles finaux exécutés, dans l'ordre demandé

Commandes depuis le worktree, chacune mesurée par `/usr/bin/time -p` :

```sh
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py unit --target tests/unit/test_reliability_followup.py::test_new_failure_and_past_recovery_survive_persisted_restarts
RUFF_CACHE_DIR=/tmp/radar-persisted-ruff-cache /Users/aminezegmou/Projets/bot-radar/.venv/bin/ruff check .
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py typecheck
make test-isolated PYTHON=/Users/aminezegmou/Projets/bot-radar/.venv/bin/python
/Users/aminezegmou/Projets/bot-radar/.venv/bin/python -B scripts/verify_consolidation.py unit --target tests/integration/test_scheduler_occurrence.py
```

| Contrôle final | Résultat exact | Durée |
| --- | --- | --- |
| Test ciblé | **1 passed**, sortie 0 | 0,10 s pytest ; 2,68 s murales |
| Ruff complet | `All checks passed!`, sortie 0 | 0,04 s murales |
| Typecheck isolé | Aucune erreur sur 162 puis 4 fichiers, sortie 0 | 16,61 s murales |
| Suite isolée complète | **3 failed, 2116 passed, 12 skipped, 4 warnings**, pytest 1 / make 2 | 71,72 s pytest ; 75,10 s murales |
| Scheduler séparé | **3 failed**, sortie 1 | 0,16 s pytest ; 2,47 s murales |

La suite collecte 2131 scénarios, soit un succès supplémentaire par rapport à la
section 10. Le nouveau test réussit dans la suite complète. Les seuls échecs sont :

- `tests/integration/test_scheduler_occurrence.py::test_the_real_scheduler_hands_the_intended_run_time_to_the_job` ;
- `tests/integration/test_scheduler_occurrence.py::test_the_transmitted_time_is_the_triggered_run_not_the_next_one` ;
- `tests/integration/test_scheduler_occurrence.py::test_a_delayed_run_is_still_identified_by_its_own_occurrence`.

La reproduction séparée conserve la signature `Scheduler réel interdit`, puis
`SchedulerNotRunningError` au nettoyage. Le scheduler, ses tests et le garde sont
inchangés ; aucune réparation ni désactivation n'entre dans ce mandat. Les rapports
d'isolation indiquent `allowed: 0` dans le parent et les 105 sous-processus de la
suite ; les trois démarrages scheduler interdits sont refusés.

Sorties temporaires : `/tmp/radar-persisted-targeted.txt`,
`/tmp/radar-persisted-targeted-final.txt`, `/tmp/radar-persisted-ruff.txt`,
`/tmp/radar-persisted-ruff-final.txt`, `/tmp/radar-persisted-typecheck.txt`,
`/tmp/radar-persisted-typecheck-final.txt`, `/tmp/radar-persisted-tests.txt` et
`/tmp/radar-persisted-scheduler.txt`.

### État et limites

Complément de couverture **terminé** ; validation complète de branche toujours
**partielle**, avec les trois échecs scheduler conservés. PostgreSQL, CI,
fournisseurs et production restent non validés. Le stockage Redis est simulé,
la remise Telegram/Redis reste non transactionnelle ; aucune garantie exactement
une fois n'est annoncée. Ni le réexamen statique ni cette preuve d'auteur ne valent
validation humaine ou autorisation de publication, fusion ou déploiement.

Branche `fix/reliable-refresh-add`, HEAD inchangé
`5cbba4c9520459ce69decebbd2bf1be2b6cc950a`, diff non committé et index vide.
Cette reprise change seulement le nouveau test et les deux rapports de fiabilité ;
les modifications applicatives préexistantes sont préservées. Aucun commit, push,
PR, merge, rebase, déploiement, migration, PostgreSQL, service réel ou modification
d'un autre worktree. Aucun secret lu ; configuration factice et transports isolés.
