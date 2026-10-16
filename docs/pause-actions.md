# Pause protégée et réconciliation

Statut : contrat actuel et procédure de contrôle. Périmètre : autorisations de pause, réconciliation et compatibilité de récupération. Les commandes à ressources exigent un mandat distinct.

Un bouton de pause porte une autorisation opaque persistée avant présentation :
propriétaire, alerte, version attendue et intention immuable (`pause` ou
`suspend_preparation`). L'émission ne prouve pas une demande utilisateur.
Les anciens boutons UUID seuls sont refusés ; `/show` fournit une nouvelle carte.

La validité est de 24 heures UTC depuis l'émission, avec borne supérieure exclue.
Le nettoyage existant purge les lignes éligibles à partir de 48 heures depuis
l'émission, lors de son prochain passage. Une preuve déjà appliquée est reconnue
même après l'expiration de l'autorisation, tant qu'elle subsiste. Une preuve
absente n'est jamais recréée et donne un résultat inconnu.

Le verrou est pris dans l'ordre alerte puis action. La version, l'admissibilité
et la validité sont relues sous verrou. Mutation, preuve update/callback et
avancement de version sont commités ensemble. Aucun appel Telegram sous verrou.
La version avance pour pause, reprise, suppression logique, remplacement pertinent
de préparation, pause temporaire et retour après préparation d'un ISBN édité,
activation finale et quarantaine. Les rafraîchissements de prix ne l'avancent pas.
La suppression physique rend l'alerte absente ; les preuves restent disponibles
jusqu'à leur purge. Une intention suspendue ne peut activer la surveillance.

Le rejeu du même update/callback reconnaît la preuve historique sans nouvelle
mutation, confirmation ou édition Telegram. Un autre clic sur le bouton consommé
est annoncé comme tel, sans prétendre appliquer une nouvelle pause. L'état courant
relu est distingué de l'application historique, notamment après une reprise.

Après retour de commit échoué, une seule relecture dans une session distincte,
bornée à deux secondes, tente d'acquérir les mêmes verrous. Une preuve correspondante
confirme l'application historique ; une autorisation non consommée et encore
admissible après résolution du verrou confirme l'absence d'application. Un conflit
plus récent, une preuve manquante, une restauration, une indisponibilité ou un délai
écoulé ne permettent pas de nier une application passée. L'annulation se propage.
Aucune mutation n'est effectuée pendant cette réconciliation.

HTTP 503 est réservé à une erreur PostgreSQL transitoire explicitement reconnue
(connexion invalidée, codes limités de connexion/verrou/sérialisation, ou délai du
pool), uniquement avant tentative de commit métier sur un bouton protégé.
Le type d'exception seul ne suffit pas : la frontière est également vérifiée.
Les autres erreurs restent HTTP 200 avec résultat explicite, comme les refus et
succès durables. Aucun contrat des autres callbacks n'est changé. Un succès métier
ne prouve ni succès global du traitement ni réception Telegram.

Le contrat de récupération cible `0014_alert_market_freshness` ; la preuve d’exécution
est bornée par le [statut de récupération](backup-recovery.md). Les archives `0012`
et `0013` sont refusées par cet outillage. La quarantaine avance toutes les versions
et invalide les actions non consommées ; les preuves appliquées demeurent historiques. Aucun
FSM n'est importé et aucune expiration ou réconciliation ne réactive une alerte.

Il n'existe ni worker, file de commandes, tâche de reprise ou relance d'envoi.
Une nouvelle invocation peut traiter une action encore valable après vérification ;
une nouvelle carte autorise une nouvelle décision. Réception initiale, nouvelle
livraison effective par Telegram et décisions absentes d'une sauvegarde restent
hors garantie. Les preuves locales ne constituent pas une garantie exactement une
fois, ni une validation de production.

Contrôles locaux :

```text
.venv/bin/python -B tests/integration/test_telegram_webhook_transient_failure.py
.venv/bin/python -B scripts/verify_pause_postgresql.py --local-checks
.venv/bin/python -B scripts/verify_pause_postgresql.py --typecheck
.venv/bin/python -B scripts/verify_pause_postgresql.py
```

La dernière commande crée uniquement une instance neuve PostgreSQL 16, à partir de
l'image locale attestée, sur `127.0.0.1:55514`, avec stockage tmpfs et configuration
Docker vide. Elle migre une véritable révision précédente peuplée et détruit son
conteneur exact. Aucune application, Redis, scheduler ou bot n'est démarré.
