# Livraison CAL sous pilote persistant

Statut : préparation locale autorisée le 18 octobre 2026. Cette procédure ne vaut
ni autorisation de merge ni mandat de production. Aucun accès d'écriture au VPS,
sauvegarde réelle, migration ou reprise réelle n'a été exécuté pour cette préparation.
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

## Conditions avant un mandat de production

Présenter la PR, sa tête exacte et les contrôles. Après accord distinct, effectuer
uniquement le merge squash et relever son SHA résultant. Le mandat de production
suivant devra nommer ce SHA, le manifeste, l'hôte `ubuntu@88.96.56.44`, la sauvegarde,
la restauration de contrôle, la migration, les réparations et le pilote.

Avant arrêt de l'application, renseigner par canal confidentiel la configuration
CAL distante et `CHASSE_PILOT_STATE_PATH=/var/lib/botradar/chasse-pilot/state.json`.
Le volume existant `alert_chain_state` contient ce chemin ; aucun volume de base ou
Redis n'est recréé. Les valeurs CAL ne sont jamais transférées par rsync ni affichées.
Identifier et suspendre sous mandat les anciens cron/scripts de collecte CAL, s'il
en existe, avant toute nouvelle collecte. Leur absence n'a pas été attestée par
les tests locaux ; une incertitude interdit de poursuivre la mise en service.

Préparer une configuration de sauvegarde privée 0600 hors Git. Ses champs sont :

- `deployment_host`, `expected_schema`, `application_revision` (ancien SHA exact),
  `output` (nouveau dossier privé durable), `gpg`, `public_home`, `private_home`,
  `recipient` (empreinte publique complète).
- `source` : `app_container`, `app_image`, `postgres_container`, `postgres_image`
  (identités complètes vérifiées), `system_id`, `database`, `user`, `database_host`,
  `database_port`, `database_url`, `docker`, `docker_environment`.

La connexion SQL doit passer par un accès borné autorisé ; l'environnement Docker
explicite vise le moteur source. La configuration ne doit jamais être imprimée.
Le destinataire et la clé de déchiffrement doivent être fournis séparément, dans
des répertoires privés ; le répertoire public ne contient aucune clé secrète.
Le pré-vol chiffre puis déchiffre une donnée aléatoire pour vérifier les clés avant
l'interruption. Aucun destinataire réel, tunnel, conteneur source ou clé de production
n'a été configuré ni validé pendant cette préparation.

## Chaîne de livraison après autorisation

Depuis le checkout du SHA résultant approuvé, utiliser `scripts/deploy.py` avec
`--pilot`, `--pilot-manifest`, `--pilot-digest` et `--pilot-backup-config` explicites.
Les fichiers privés restent sur le poste de contrôle. Ne pas lancer `make deploy`
sans ces arguments pour cette livraison.

L'ordre exécutable est : pré-vol et clés → capture/tag de l'ancienne image et
identités PostgreSQL/Redis → arrêt confirmé de l'app → sauvegarde et restauration
de contrôle → synchronisation → construction → activation persistante du pilote
dans une commande ponctuelle sans dépendances → migration → démarrage et contrôles.
Un échec interrompt la chaîne. Aucun retour ou restauration automatique.

La sauvegarde exige l'application arrêtée et PostgreSQL inchangé. Le dump custom
et les empreintes de toutes les tables partagent le même instantané MVCC en lecture
seule. La révision réelle 0014 ou 0015 doit correspondre à la configuration ; elle
n'est jamais forcée à la tête. Le bundle chiffré conserve le dump, les empreintes,
les identités et l'ancienne image, sans secrets CAL ni configuration de connexion.
Le marqueur COMPLETE est écrit en dernier. Limite de cette procédure : 512 Mio
pour le dump et l'archive ; aucune durée d'interruption n'est garantie.

Avant migration, le script restaure cette archive dans un PostgreSQL 16 jetable
local : image déjà présente, port 127.0.0.1:55514, tmpfs, aucune donnée persistante.
Il refuse le port occupé, une cible non vide, une identité différente, une archive
altérée ou une clé incorrecte. Toutes les empreintes et la révision doivent être
identiques ; le reçu privé `RESTORE_CHECK.json` atteste cette vérification sans
libération métier. Le conteneur est supprimé même en cas d'échec. La source doit
encore être arrêtée et identique à l'instantané après cette vérification.
Cette restauration de contrôle ne lance pas l'application et ne met aucune alerte
en pause : elle ne modifie pas les statuts du dump. Elle est distincte de l'ancien
outillage synthétique avec quarantaine décrit dans [backup-recovery](../backup-recovery.md).

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

## Exécution et contrôles du pilote sous mandat distinct

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

## Retour et décision de sortie

En cas d'échec, maintenir le pilote ou l'app arrêtée et conserver les preuves. Ne
pas relancer automatiquement l'ancienne image : elle ignore le nouveau verrou de
maintenance. La compatibilité de l'ancienne image avec les colonnes additives 0015
et les données post-pilote doit être examinée avant un retour applicatif autorisé.
Le tag de retour est celui capturé et vérifié avant build, jamais `latest` supposé.
Un downgrade supprimerait les nouvelles colonnes : il n'est pas le retour par défaut.

La restauration destructive de production exige un mandat séparé nommant l'archive,
la cible, la perte de données acceptable et les contrôles. Le script de restauration
de contrôle refuse la source ; il ne fournit aucun raccourci de restauration réelle.
Ni le pilote ni son échec n'autorisent une restauration destructive.

La sortie du pilote demande un autre mandat explicite. La commande `release`
exige l'empreinte et `--authorize-normal-operation` ; elle réouvre immédiatement
les entrées et les jobs enveloppés. Le poller ne démarre qu'au prochain démarrage
normal : planifier ensemble la levée de maintenance et ce redémarrage. Aucun de
ces gestes ne fait partie du pilote. Sans nouvel accord, l'application reste sous
maintenance pour une durée indéfinie et les notifications administratives restent
bloquées ; le bilan est communiqué dans cette conversation.
