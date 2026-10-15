# Sauvegarde et restauration : outillage synthétique

Périmètre : contrat et procédure de l'outillage de sauvegarde/restauration ; les exercices datés restent historiques. Lire [pause-actions](pause-actions.md) pour le schéma courant et la quarantaine.

Statut : vérifié sur données synthétiques uniquement. Aucune sauvegarde indépendante,
planification, rétention distante ou restauration de production n'est activée.

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

## Objectifs non activés

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
les copies jusqu'à leur échéance. Aucun cron n'a été installé.

## Restauration sous maintenance

La cible de l'exercice est un PostgreSQL 16 neuf, sans volume existant, publié sur un
port 127.0.0.1 exact. Le label, le conteneur, les ports, le stockage, l'identifiant
système SQL, la version, la base et l'utilisateur sont attestés. Une cible non vide,
une identité discordante ou une version incompatible sont refusées avant pg_restore.
L'outil courant prend exclusivement en charge `0013_pause_actions` (`SCHEMA` dans
`scripts/backup_recovery.py`). Les preuves historiques 0012 nécessitent leur ancien
kit compatible. Il ne contourne pas `freeze_bd_preparations.py`, réservé à 0010. L'archive originale reste intacte.

Après restauration et contrôle de toutes les empreintes :

- Les marqueurs notifiés et leurs reçus/dates restent inchangés.
- ready/deferred/sending sans notified_at deviennent unknown ; aucune preuve d'envoi
  n'est reconstruite. Les unknown antérieurs et abandoned sont préservés.
- Les alertes ACTIVE passent en PAUSED.
- Les préparations gardent contexte, état et dates ; leur intention d'activation est
  retirée et leur jeton renouvelé. Une passe réelle de reprise des préparations est
  testée, sans réactivation.
- Aucun FSM ni contenu Redis n'est importé ; les dialogues antérieurs sont abandonnés.

Aucun mode de libération métier n'existe. L'application normale ne doit pas démarrer
pendant ces opérations : son lifespan contacte Telegram et lance le scheduler.
Maintenir fermées les entrées Telegram/ingestion. Revoir explicitement suppressions,
modifications, utilisateurs/allowlist et intentions de préparation avant reprise.
Une annonce entièrement absente de la copie peut revenir comme nouvelle : mettre
seulement les pending en unknown ne suffit pas. Les alertes restaurées restent en
pause ; l'exercice prouve zéro envoi dans cet état, pas après leur réactivation.

## Commandes locales de l'exercice

```bash
.venv/bin/python -B scripts/exercise_backup_recovery.py --unit
.venv/bin/python -B scripts/exercise_backup_recovery.py --typecheck
bash scripts/backup_db.sh --synthetic-exercise
```

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
vérifiée ; les fichiers effectivement déployés par bind mounts devront être attestés.
L'exercice local ne valide ni transfert distant, coût, authentification réelle,
volumes réels, quatre heures de restauration, ni reprise métier exactement une fois.
