# Retrait du dispositif pilote CAL

Statut : procédure préparée le 19 octobre 2026 ; aucune exécution de production
par cette documentation. La livraison exige le mandat au SHA exact fusionné,
selon [déploiement](deployment.md). Aucun nouveau mode de maintenance.

## Effets de la version

Le runtime ne connaît plus le verrou, le manifeste, les compteurs ni les commandes
d'activation/reprise/sortie. Un ancien fichier d'état, même arrêté ou corrompu,
est ignoré. La migration 0015 et toutes les données métier restent inchangées.
L'application configure Telegram au démarrage, démarre APScheduler et lance le
poller seulement si sa configuration normale l'active. Les protections d'accès,
les limites de concurrence et les reprises CAL restent en place. L'erreur CAL
`search_results` n'est pas résolue par ce retrait.

## Nettoyage ciblé lors de la livraison autorisée

1. Vérifier le SHA livré et l'arrêt confirmé de l'app dans la chaîne canonique.
   Retirer uniquement la ligne `CHASSE_PILOT_STATE_PATH` de la configuration distante
   et ses éventuelles surcharges Compose identifiées. Ne pas afficher les valeurs
   de configuration ni modifier les secrets CAL ou les options normales du poller.
2. Dans le volume applicatif existant, retirer uniquement le répertoire
   `/var/lib/botradar/chasse-pilot/` après vérification de son chemin et de son
   contenu. Ne supprimer ni le volume `alert_chain_state`, ni ses autres fichiers,
   ni les volumes PostgreSQL/Redis. Les preuves privées et le manifeste local
   peuvent être conservés comme historique ; aucun code ne les utilise désormais.
3. Maintenir neutralisé l'ancien cron CAL déjà identifié : la maintenance de
   session et les prix sont pris en charge par l'unique scheduler applicatif.
   Ne pas réactiver ce cron ni modifier d'autres tâches système.
4. Livrer via `scripts/deploy.py`, sans argument pilote ni backfill. Le déployeur
   garde l'ordre arrêt, sync, build, migration, démarrage, vérification et ses
   contrôles d'identité PostgreSQL/Redis. Après rsync et avant le build, il supprime
   seulement sept fichiers obsolètes explicitement nommés : les modules
   `app/core/pilot_mode.py`, `app/utils/pilot_manifest.py`, la commande
   `scripts/chasse_pilot.py`, ses trois fichiers de tests et l'ancienne procédure
   `docs/operations/chasse-pilot-release.md`. Aucun `rsync --delete` général.
   Un échec de synchronisation ou de retrait interdit le build et le démarrage.
   Aucun downgrade ni effacement de cache.
   La notification ordinaire de livraison redevient disponible ; son émission
   doit être comprise dans le mandat de livraison.
5. Vérifier santé, commandes Telegram, accès ingestion protégé, présence des jobs
   et démarrage du poller selon sa configuration. Vérifier qu'aucun fichier pilote
   n'est recréé après redémarrage. Ne pas envoyer d'annonce de test sans mandat.

Le redémarrage rétablit les tâches normales, y compris les petits lots de prix
éligibles à sept jours et les notifications normales. Il ne lance pas le script
manuellement ni une reprise massive des 116 éditions. Cette activité automatique
normale doit être annoncée dans le mandat avant livraison. Si elle doit rester
limitée, obtenir un arbitrage avant livraison plutôt que recréer un verrou pilote.

## Diagnostic et reprise ponctuelle

`python scripts/chasse_prices.py render --isbn ISBN` lit le cache sans collecte ni
Telegram. `probe --isbn ISBN --authorize-source-access` exige un mandat réseau et
vérifie l'historique certifié et la persistance de session Redis, sans prix forcé,
sans écrire de prix ni bloquer le bot. Deux processus peuvent vérifier la réutilisation
de session. Une restriction ou une perte d'accès interrompt la sonde concernée.

La [reprise par inventaire](chasse-weekly-prices.md) reste disponible sous mandat
explicite, avec empreinte, revalidation bibliographique et petits lots. Elle ne
nécessite aucun arrêt global. Aucun de ces outils n'est exécuté implicitement au
déploiement. L'automatisation de l'ancien pilote reste en pause.
