# Sauvegarde et restauration : outillage synthétique

Périmètre : contrat et procédure de l'outillage de sauvegarde/restauration ; les exercices datés restent historiques. Lire [pause-actions](pause-actions.md) pour le schéma courant et la quarantaine.

Statut : les preuves datées ci-dessous restent celles du schéma 0014. Le code
courant de l’outillage synthétique est aligné sur 0015. Les essais isolés CAL aux
schémas 0014 et 0015 restent des preuves historiques. Le
[pilote CAL](operations/chasse-pilot-release.md) exclut désormais cet outillage de
son parcours de livraison, sur instruction du propriétaire. Aucune sauvegarde
indépendante, planification, rétention distante ou restauration de production
n'est activée.

## Contrat

Le dump complet PostgreSQL custom utilise un instantané MVCC exporté. Les empreintes
et nombres de lignes sont lus dans la même transaction repeatable-read ; des écritures
concurrentes ne doivent pas produire un manifeste portant sur un autre instant.
`snapshot_bound` est une borne UTC capturée avant cette transaction. Le délai de
transfert ne rajeunit jamais l'archive. Une restauration vide doit retrouver toutes
les lignes/colonnes/date/relations avant toute mutation de quarantaine.

Le contenu est empaqueté puis chiffré OpenPGP. GnuPG utilise exclusivement un homedir
explicite, sans configuration personnelle ni récupération automatique de clé.
L'exercice distingue le répertoire public utilisé pour sauvegarder du répertoire de
clé privée utilisé pour restaurer. Il vérifie l'absence de clé secrète dans le premier.
Le manifeste indique l'empreinte publique du destinataire, le format, la version
PostgreSQL, la source attestée, la révision applicative et les empreintes du kit.

Le kit lit une liste blanche Git : code Python app/alembic, scripts versionnés
Python/shell, Dockerfile, Compose, Alembic, pyproject et requirements.lock. Il n'inclut
ni `.git`, `.env`, données locales, clés, exports ni documents personnels. Le noyau
applicatif et les migrations sur disque doivent correspondre à la révision Git.
L'outillage courant est attesté séparément dans `tools/` ; certains scripts historiques
archivés dans `kit/` ne constituent pas une procédure de restauration autorisée.
Le kit de configuration est factice : il ne permet aucun démarrage de production.

## États annoncés et succès

- `local_archive` : dump lisible et bundle chiffré produits localement.
- `simulated_verified` : publication locale distincte, taille et empreinte vérifiées.
- `independent_verified` reste toujours false : aucun transport indépendant n'existe
  dans cet outillage. Une simulation fraîche ne rend pas le contrôle opérationnel vert.

La publication crée un identifiant UUID exclusif ; une copie existante n'est jamais
écrasée. Le marqueur COMPLETE n'est écrit qu'après tous les fichiers. Une publication
échouée reste sans ce marqueur et ne supprime aucune copie antérieure.
La lisibilité, le chiffrement et les empreintes ne remplacent pas une restauration.
Ils ne prouvent pas à eux seuls l'origine d'une archive contre un attaquant pouvant
fabriquer une nouvelle archive avec la clé publique. Les archives de confiance et
les permissions du dépôt indépendant restent des prérequis opérationnels.

## Objectifs non activés par cet outillage

- Deux passages quotidiens, 04:00 et 16:00 **UTC**.
- Copies quotidiennes : expiration à `snapshot_bound + 14 jours`.
- Une représentante complète par semaine UTC (lundi 00:00), la plus récente ; huit
  semaines calendaires au plus. Expiration hebdomadaire au lundi de cette semaine
  plus huit semaines, ou à l'échéance quotidienne si celle-ci est plus tardive.
- Les huit dernières semaines signifient la semaine courante et les sept précédentes ;
  les semaines manquantes ne sont pas comblées avec des copies arbitrairement anciennes.
- Alerte au-delà de 18 heures depuis la borne d'instantané ; copie absente/incomplète
  également en échec. `retention-plan` ne supprime rien.
- RPO visé 24 heures ; RTO technique visé quatre heures, cible encore sous maintenance.

Ces durées ne sont pas mesurées en production. Les rétentions de sauvegarde sont
indépendantes des purges applicatives : des données supprimées peuvent rester dans
les copies jusqu’à leur échéance. Ces recettes n’installent aucun cron ; l’état
d’un éventuel ordonnanceur de production n’a pas été inspecté.

## Restauration sous maintenance

La cible de l'exercice est un PostgreSQL 16 neuf, sans volume existant, publié sur un
port 127.0.0.1 exact. Le label, le conteneur, les ports, le stockage, l'identifiant
système SQL, la version, la base et l'utilisateur sont attestés. Une cible non vide,
une identité discordante ou une version incompatible sont refusées avant pg_restore.
L’outil synthétique courant accepte exclusivement `0015_chasse_weekly_prices`, valeur de `SCHEMA`
dans [`scripts/backup_recovery.py`](../scripts/backup_recovery.py), alignée sur
l’unique tête Alembic. Les archives historiques 0012, 0013 et 0014 sont refusées par cet outil courant : ne pas changer leur
manifeste ou la constante pour les faire accepter. Le test statique d’alignement
ne remplace pas une restauration PostgreSQL vérifiée.
Il n’y a pas de migration implicite pendant la restauration. La procédure 0010
(`delivery_prerequisites.py` et `freeze_bd_preparations.py`) n’est pas un repli
compatible. L’archive originale reste intacte.

Après restauration et contrôle de toutes les empreintes :

- Les marqueurs notifiés et leurs reçus/dates restent inchangés.
- ready/deferred/sending sans notified_at deviennent unknown ; aucune preuve d'envoi
  n'est reconstruite. Les unknown antérieurs et abandoned sont préservés.
- Toutes les alertes voient leur `decision_version` incrémentée ; les alertes
  ACTIVE passent en PAUSED.
- Les actions de pause non appliquées (`pause_actions.applied_at IS NULL`) sont
  invalidées. Les preuves des actions déjà appliquées sont conservées ; une ancienne
  action en attente ne peut pas servir à réactiver la cible restaurée.
- Les préparations gardent contexte, état et dates ; leur intention d'activation est
  retirée et leur jeton renouvelé. L’exercice appelle le vrai use case de reprise
  sur les données synthétiques, sans réactivation ; il ne valide pas une reprise
  de production.
- Aucun FSM ni contenu Redis n'est importé ; les dialogues antérieurs sont abandonnés.

Aucun mode de libération métier n’existe. Les fonctions `restore` et
`quarantine_connection` n’arrêtent pas l’application et ne ferment pas ses entrées :
la maintenance est une **précondition opérateur**, pas une garantie fournie par ces
fonctions. Le libellé d’erreur « cible maintenue arrêtée » ne réalise aucun arrêt.
L’application normale ne doit pas démarrer pendant ces opérations : son lifespan
contacte Telegram et lance le scheduler.
Maintenir fermées les entrées Telegram/ingestion. Revoir explicitement suppressions,
modifications, utilisateurs/allowlist et intentions de préparation avant reprise.
Une annonce entièrement absente de la copie peut revenir comme nouvelle : mettre
seulement les pending en unknown ne suffit pas. Les alertes restaurées restent en
pause ; le scénario synthétique vérifie zéro envoi simulé dans cet état, pas après
leur réactivation. Un échec peut laisser des données restaurées ou une quarantaine
incomplète : garder la cible sous maintenance, constater schéma/données/état des
producteurs et décider de la suite. Aucun rollback de données, retour à l’ancien
code ou redémarrage automatique n’est fourni par cet outillage.

## Commandes locales de l’exercice

Ces commandes sont des points d’entrée, **pas une autorisation d’exécution**. Les
deux premières ne lancent pas de base. La troisième crée les ressources décrites
ci-dessous et demande une permission distincte ; ne pas la lancer pour vérifier une
simple modification documentaire.

```bash
.venv/bin/python -B scripts/exercise_backup_recovery.py --unit
.venv/bin/python -B scripts/exercise_backup_recovery.py --typecheck
bash scripts/backup_db.sh --synthetic-exercise
```

`backup_recovery.py` n’expose en CLI que `verify`, `freshness` et `retention-plan`.
Ces lectures ne restaurent rien. `backup` et `restore` sont appelés par l’exercice
avec ses identités explicites ; aucune commande de restauration de production n’est
fournie. Sans `--synthetic-exercise`, `backup_db.sh` sort avec le code 2 : son appelant
doit rendre ce refus visible. Le code de sortie d’un cron réel n’a pas été inspecté.

Les imports et la collecte sont précédés de barrières dotenv/réseau. Les connexions
Python sont limitées à 127.0.0.1:55520 et :55521 ; les sondes négatives utilisent :55522
et une adresse de documentation, sans contact. Docker utilise une configuration
vide dédiée et un socket explicitement désigné, jamais Compose général. Les deux
conteneurs sont créés depuis l'image PostgreSQL 16 déjà disponible, sans pull.
Les clés GnuPG, agents, archives et ressources sont jetables ; suppression uniquement
par leurs identifiants créés dans cette exécution, y compris après échec.

Les événements post-sauvegarde utilisent le vrai use case avec Telegram simulé : deux
accusés exploitables, un résultat inconnu, puis modification/suppression. Une annonce
absente de la sauvegarde est réintroduite sur la cible en quarantaine. Ces effets
postérieurs ne sont pas récupérés par la copie et ne sont pas prétendus absents.

## Vérification locale du 16 octobre 2026

Base : `54ad9f8212ea0b76a161a481146c304df7c98396`, avec le diff local du lot de
récupération 0014, sans commit. Interpréteur existant :
`/Users/aminezegmou/Projets/bot-radar/.venv/bin/python`, depuis le worktree
`/Users/aminezegmou/Projets/bot-radar-worktrees/backup-recovery-schema`.

- `scripts/exercise_backup_recovery.py --unit` : 20 tests réussis ; le décompte
  précédent de 16 augmente d’un test d’alignement et de trois refus de schéma
  (0012, 0013 et inconnu). L’alignement a échoué avant correction de `SCHEMA`.
- `scripts/exercise_backup_recovery.py --typecheck` : zéro erreur sur les deux scripts.
- `ruff check .` et le typage `app scripts/matching_diagnostics.py` : zéro erreur.
- `make test-isolated` : 1 928 réussis, 12 ignorés, quatre avertissements de
  dépréciation. Cette sélection sans PostgreSQL ne valide pas les scénarios ignorés.
- `scripts/exercise_backup_recovery.py`, avec environnement initial vide et outils
  locaux explicites : réussite sur deux PostgreSQL 16 neufs, image locale épinglée,
  ports 55520/55521 et stockage tmpfs. Aucun téléchargement effectué.

L’exercice vérifie avant sauvegarde les cinq champs ajoutés par 0014 : valeurs
JSON et dates discriminantes, valeurs NULL permises et `is_historical` vrai/faux.
Après restauration, les empreintes de toutes les tables publiques sont identiques
avant quarantaine. Les comparaisons de lignes après quarantaine couvrent deux
alertes, six notifications (dont un reçu confirmé complet et un état abandoned),
deux actions de pause, deux caches, deux observations et une liaison alerte/livre.
Seules les mutations prévues de quarantaine sont admises ; la reprise des
préparations ne réactive aucune alerte et aucun envoi simulé ne suit la restauration.

Les neuf contrôles négatifs existants réussissent : nouvelle sauvegarde échouée,
mauvaise clé, mauvaise cible, mauvaise révision, écrasement, corruption, troncature,
copie incomplète et cible non vide. Les deux conteneurs sont supprimés par leurs
identifiants exacts ; les répertoires temporaires, clés et agents GnuPG de l’exercice
sont nettoyés. Aucune ressource préexistante n’est modifiée. Cette preuve ne valide
ni une sauvegarde indépendante, ni une restauration de production, ni RPO/RTO.

## Prérequis d'une exploitation réelle

Un mandat distinct devra définir la destination indépendante, une identité de source
réelle attestée, un adaptateur de transport et ses contrôles, ainsi que les droits de
restauration. Le compte de dépôt ne doit pas pouvoir remplacer/supprimer les anciennes
copies. Il faut un contrôle d'ancienneté hors serveur et un responsable de ses alertes.

La clé privée de récupération doit rester hors serveur en deux exemplaires protégés,
avec accès testé et personnes responsables. La rotation conserve les anciennes clés
jusqu'à expiration des copies concernées. Conserver aussi les accès au stockage et
la configuration réelle dans un kit protégé hors serveur ; ne jamais les ajouter au
kit factice ni aux logs. Aucun achat ou fournisseur n'est choisi ici.

Le traitement actuel charge dump, bundle et empreintes en mémoire : le volume et les
ressources réels doivent être mesurés, et le streaming évalué avant exploitation.
La révision du code et le verrou de dépendances ne sont pas une image reconstruite
vérifiée. Compose utilise désormais le code embarqué par le Dockerfile ; le contrôle
après déploiement compare les fichiers versionnés dans le conteneur actif. Cette
comparaison ne prouve ni une reconstruction reproductible de l’image ni une
restauration réussie ; ces validations restent distinctes.
L'exercice local ne valide ni transfert distant, coût, authentification réelle,
volumes réels, quatre heures de restauration, ni reprise métier exactement une fois.
