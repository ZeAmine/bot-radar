# Livraison CAL sous pilote persistant

Statut : procédure du chantier après abandon explicite du prérequis de sauvegarde
par le propriétaire. Cette page ne donne aucune permission supplémentaire. La
modification du déployeur doit être validée par PR avant son utilisation.
Le [déploiement canonique](deployment.md) reste la seule chaîne de livraison.

## Périmètre privé figé

Le manifeste durable est conservé hors Git, dans
`/Users/aminezegmou/Projets/bot-radar-private/chasse-weekly-prices/pilot-manifest.json`
(répertoire 0700, fichier 0600). Son empreinte SHA-256 canonique, calculée après
retrait du champ `digest` et sérialisation JSON avec clés triées, est :
`32ebfadebe3ea9b1e5b5afe4e66ba17949f73e7202578faedbbd38bba68ed36c`.
Elle n'est pas l'empreinte des octets du fichier indenté.

Le manifeste porte sur cinq alertes et sept éditions au maximum : Jean Doux
9782756079332 ; Blacksad T1 9782205049657, T2 9782205051995 et T3 luxe 9782205059250 ;
Nanbanjin 9782808500487 ; Minor Arcana T2 9782413091424 ; Tout ce qui meurt & agonise
9791026856405. Les trois réparations et leurs preuves figurent dans la
[procédure de reprise](chasse-weekly-prices.md).

Blacksad 2205059254 est relié au livre canonique 9782205059250 pour les seules
alertes approuvées. Les anciennes fiches, caches et observations sont conservés,
sans fusion des minima et sans fusion avec le T3 standard 9782205055641.
Aucun critère utilisateur ni statut actif/en pause n'est modifié.

L'inventaire historique compte 142 alertes et 274 livres liés ; ce ne sont pas des
nombres de collectes. Le périmètre confirmé proposé compte 123 éditions uniques,
dont sept pour ce pilote et 116 réservées à un mandat ultérieur. Les associations
incertaines des autres éditions de séries restent exclues des corrections.

## Mandat et préconditions de livraison

La cible de production est le commit de `main` explicitement autorisé par le
propriétaire, avec le manifeste approuvé, sur `ubuntu@88.96.56.44`. Le mandat doit
couvrir les opérations prévues : migration, réparations démontrées et pilote.
Une PR ne remplace pas implicitement la cible par le dernier `main` : toute nouvelle
cible livrée doit être explicitement validée. Le merge conserve sa confirmation
séparée au SHA exact.

Avant arrêt de l'application, renseigner par canal confidentiel la configuration
CAL distante et `CHASSE_PILOT_STATE_PATH=/var/lib/botradar/chasse-pilot/state.json`.
Le volume existant `alert_chain_state` contient ce chemin ; aucun volume de base ou
Redis n'est recréé. Les valeurs CAL ne sont jamais transférées par rsync ni affichées.
Identifier et suspendre sous mandat les anciens cron/scripts de collecte CAL, s'il
en existe, avant toute nouvelle collecte. Leur absence n'a pas été attestée par
les tests locaux ; une incertitude interdit de poursuivre la mise en service.

Le propriétaire a retiré de ce chantier l'obligation de sauvegarde, de GPG et de
restauration de contrôle. Ces opérations ne font plus partie du pré-vol ni de la
chaîne de livraison, et aucun résultat réussi ne leur est attribué. Le contrôle du
manifeste et les autres protections du déploiement restent obligatoires.

## Chaîne de livraison après autorisation

Depuis le checkout du SHA résultant approuvé, utiliser `scripts/deploy.py` avec
`--pilot`, `--pilot-manifest` et `--pilot-digest` explicites.
Le manifeste privé reste sur le poste de contrôle. Ne pas lancer `make deploy`
sans ces arguments pour cette livraison.

L'ordre exécutable est : pré-vol et manifeste → capture/tag de l'ancienne image et
identités PostgreSQL/Redis → arrêt confirmé de l'app → synchronisation →
construction → activation persistante du pilote dans une commande ponctuelle sans
dépendances → migration → démarrage et contrôles.
Un échec interrompt la chaîne. Aucun retour ou restauration automatique.

La migration 0015 ajoute seulement `source_history`, `refresh_state` et
`next_refresh_at` au cache, avec un index d'échéance. Aucun prix, date ou preuve
historique n'est inventé ; aucun ancien enregistrement n'est supprimé.
La migration et l'activation utilisent `--no-deps` ; le démarrage vérifie les
identités et dates de démarrage PostgreSQL/Redis relevées avant arrêt.
La notification Telegram habituelle de déploiement est supprimée pendant le pilote.

## État des services et absence de sortie automatique

| Moment | État effectif |
| --- | --- |
| Avant arrêt | Ancienne application inchangée ; aucun pilote exécuté pendant la préparation locale. |
| Arrêt jusqu'au démarrage cible | App, ingestion, bot, poller et scheduler arrêtés ; PostgreSQL et Redis restent en service. |
| Application cible sous pilote | API vivante ; santé et GET internes authentifiés disponibles. Autres entrées HTTP en 503, dont ingestion et webhook Telegram. Poller non démarré. |
| Scheduler cible | APScheduler démarre dans l'unique processus, mais tous les jobs sont neutralisés, y compris actualisations, enrichissements, reprises de notifications, nettoyage et maintenance de session. |
| Telegram | Tous les appels du bot sont bloqués, y compris administration, configuration des commandes et notification de déploiement. Aucun message commercial ou de test. |
| Fin, erreur, redémarrage ou propriétaire absent | Le pilote reste actif sans échéance. Aucun traitement des 116 éditions, aucune réouverture de l'ingestion et aucun envoi automatique. Seuls santé, lectures internes, logs et commandes manuelles bornées restent disponibles. |

Le fichier d'état et son manifeste embarqué sont persistants. L'état manquant
lorsqu'un chemin est configuré, ou corrompu, ferme les accès. Le compteur conserve
les éditions déjà tentées, y compris après interruption ; réactiver le même manifeste
ne le remet pas à zéro. Une collecte commencée et interrompue exige un examen
opérateur avant toute nouvelle collecte. Aucune boucle de reprise ne peut étendre
le périmètre.

## Exécution et contrôles du pilote autorisé

Dans le conteneur cible, `scripts/backfill_chasse_prices.py --apply --pilot-state
/var/lib/botradar/chasse-pilot/state.json --approved-digest EMPREINTE --limit 7`
relit le manifeste embarqué ; aucune seconde copie n'est nécessaire. Une invocation
est séquentielle et mutualise les éditions. Une nouvelle invocation n'effectue pas
une seconde collecte d'une édition déjà tentée. Elle ne supprime aucun ancien livre,
cache ou observation. Les réparations sont conditionnelles aux anciennes liaisons
exactes ; un écart arrête la réparation concernée.

Arrêt immédiat sur sortie de périmètre, session indisponible, restriction source ou
interruption ; arrêt sur deux échecs consécutifs de même signature. Toute anomalie
opérationnelle non couverte impose aussi l'arrêt manuel et l'examen des logs. Aucun
réarmement automatique de l'état arrêté n'est prévu. Une identité non démontrée
reste isolée, sans attribution de prix à un autre livre.

Contrôles avant et après chaque étape : comparer critères/statuts des cinq alertes,
anciennes fiches/caches/observations conservés, exactement sept éditions au maximum,
aucune tentative hors manifeste, source/minimum/période/état/frais certifiés, dates
indépendantes des offres et de l'historique, échéances persistées et futures.
La cadence de sept jours est programmée, mais les jobs ne l'exécutent pas tant que
la maintenance pilote demeure active. Ne pas annoncer une reprise hebdomadaire en
production à partir de ces seuls tests locaux.

`scripts/chasse_pilot.py status --state CHEMIN` contrôle le verrou et le compteur.
`render --state CHEMIN --isbn ISBN` produit localement le panneau du cache, sans
Telegram ni collecte. Comparer les panneaux de tous les tomes et leur contexte,
puis vérifier que leurs lectures n'ont pas modifié le compteur. La recette cible
ne clique pas un bouton Telegram : le webhook est volontairement fermé.
Le chemin normal du clic sans collecte est prouvé séparément par les tests.

Après collecte de Jean Doux, `probe --state CHEMIN --isbn 9782756079332` vérifie
l'historique authentifié et le stockage dans le Redis réellement configuré, sans
imprimer le jeton. Deux sondes au maximum permettent deux processus distincts : la
seconde doit retrouver la session sans reconnexion. Une preuve d'authentification ou
de persistance absente arrête le pilote. Comparer le minimum effectivement obtenu,
stocké et rendu ; 9,36 € est une observation historique, jamais une valeur forcée.
La conservation lors d'un échec est testée en environnement isolé ; ne pas révoquer
le vrai compte ni provoquer volontairement un échec réseau en production sans mandat.

## Reprise manuelle après une restriction temporaire

Une recherche publique redevenue accessible ne prouve pas que le compte ou
l'historique fonctionnent. Ne pas supprimer le fichier d'état, effacer un essai,
changer le manifeste ou contourner une page de vérification pour reprendre.

Après diagnostic et autorisation explicite, une seule reprise opérateur est
possible pour une édition dont la collecte est en échec. La commande lit
`next_refresh_at` dans le cache en transaction READ ONLY et refuse une échéance
absente ou future. Elle ne force ni prix ni date, et ne contacte pas la source :

```sh
docker compose exec -T app python scripts/chasse_pilot.py resume \
  --state /var/lib/botradar/chasse-pilot/state.json \
  --approved-digest EMPREINTE_APPROUVEE --isbn ISBN_APPROUVE \
  --authorize-pilot-retry
```

Cette commande conserve le manifeste, les sept éditions uniques et le premier
bilan. Elle autorise au plus un second essai pour la seule édition indiquée ;
les éditions déjà réussies restent exclues. L'autorisation survit au redémarrage,
mais ne peut pas être renouvelée. Une collecte interrompue, une violation de
périmètre ou un second incident demandent un nouvel examen opérateur, sans
réarmement automatique. Reprendre ensuite uniquement la commande de backfill
bornée du pilote et arrêter de nouveau sur les critères source existants.

Les étapes, fournisseurs, opérations et codes HTTP des tentatives sont conservés
dans l'état pilote et les logs structurés, sans URL de requête ni données de
session. Après une panne technique de recherche ISBN, aucun repli par titre n'est
lancé ; le repli reste disponible après une recherche valide sans résultat ou un
rejet bibliographique. Les restrictions sont reconnues aux différentes étapes
avant de poursuivre vers les offres ou l'historique.

La reprise maintient le mode pilote : ingestion, Telegram, poller et tâches
automatiques restent suspendus. Elle ne constitue pas une sortie de maintenance.

## Retour et décision de sortie

En cas d'échec, maintenir le pilote ou l'app arrêtée et conserver les preuves. Ne
pas relancer automatiquement l'ancienne image : elle ignore le nouveau verrou de
maintenance. La compatibilité de l'ancienne image avec les colonnes additives 0015
et les données post-pilote doit être examinée avant un retour applicatif autorisé.
Le tag de retour est celui capturé et vérifié avant build, jamais `latest` supposé.
Un downgrade supprimerait les nouvelles colonnes : il n'est pas le retour par défaut.

Aucune restauration automatique ni opération destructive n'est autorisée en cas
d'échec. Arrêter les opérations concernées et constater l'état avant de décider
la suite avec le propriétaire.

La sortie du pilote demande un autre mandat explicite. La commande `release`
exige l'empreinte et `--authorize-normal-operation` ; elle réouvre immédiatement
les entrées et les jobs enveloppés. Le poller ne démarre qu'au prochain démarrage
normal : planifier ensemble la levée de maintenance et ce redémarrage. Aucun de
ces gestes ne fait partie du pilote. Sans nouvel accord, l'application reste sous
maintenance pour une durée indéfinie et les notifications administratives restent
bloquées ; le bilan est communiqué dans cette conversation.
