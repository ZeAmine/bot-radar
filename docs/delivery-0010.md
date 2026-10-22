# Livraison préparée : sauvegarde 0010 et maintenance Radar

Statut : dossier historique de préparation et attestations datées, pas procédure courante. Périmètre : livraison 0010 ; les prérequis non exécutés restent tels que qualifiés ci-dessous. Utiliser [le déploiement actuel](operations/deployment.md) pour toute nouvelle opération autorisée.

Cette procédure reste arrêtée avant le GO de phase B. Le complément ne modifie ni
application, migration, lanceur sécurisé ni fixture partagée. Il n’active pas une
sauvegarde indépendante planifiée. Les commandes de données réelles ci-dessous
ne sont **pas** exécutées pendant la préparation.

## Identités de départ et prérequis

Référence consolidée initiale : db6d69920edc57adab65d6d1fa458f4be2848a85 ;
application inchangée : bfabcca7dbf4f8f6af2de058b167a93dd80816b8.
Le complément publié change la tête de la PR #95 : le SHA à approuver sera son
nouveau head, pas db6d699. Recontrôler CI, base distante et contenu avant merge.

Source constatée : PostgreSQL 16.15, schéma `0010_alert_preparation`, system_id
`7683840515455610913`, conteneur
`72d9a4d805919682ddfe053c2b1fd9eea0f482b1f6005943a911129b23957935`.
App source : `5f3896fb62cfbbc1abee368b635803e10ae5cfa285a8280c38225ae4c9a4425a`,
image `sha256:e59ff3589ff50b37ea5cac131337e66973172354956dde3f44b14f770933fa2c`.
Code compatible vérifié : `7852c3d4f85da9636517ee95f1d639f627e13ad6` ; ce marqueur
n’est pas utilisé seul : les fichiers hôte/runtime comparables ont été empreintés.
Revalider ces identités après GO ; toute différence impose une nouvelle préparation.

Mac : `/Users/aminezegmou/BotRadar-recovery/` (0700), copies sous `backups/<UUID>/`,
trousseau privé `private-gnupg-v2/`, attestations sous `procedures/`. Serveur :
`/home/ubuntu/bot-radar-recovery/{public-gnupg,staging,docker-client}/` (0700).
Le dossier docker-client est vide et le socket Unix explicite. Aucun trousseau ou
fichier existant n’est remplacé. Aucun homedir personnel implicite GnuPG utilisé.

Création privée : intervention locale dans Terminal et Pinentry, jamais phrase
secrète dans argv, conversation ou logs. L’utilisateur choisit sa phrase et confirme
la génération. Une clé RSA 3072 de chiffrement valable un an est proposée ; son
expiration ne retire pas la capacité de déchiffrer les copies anciennes. La rotation
ne permet pas de supprimer les anciennes clés tant que leurs archives existent.
Seule la clé publique peut aller sur le serveur, avec empreinte primaire vérifiée et
absence de clé secrète dans son trousseau. Avant GO, un nonce exclusivement factice
doit être chiffré/déchiffré pour attester l’accès privé, sans publier la phrase.

Seconde copie : support physique distinct chiffré/offline à fournir par Amine.
Comparer son identité de disque avec celle du Mac (une autre partition du même
disque n’est pas indépendante). Exporter la clé privée protégée par sa phrase via
GnuPG dans ce support 0700/0600, jamais dans le dépôt ou le serveur. Vérifier sa
lisibilité dans un trousseau temporaire **privé hors serveur**, puis conserver cette
copie ; ne supprimer aucune clé réelle pendant le nettoyage. Le support, sa protection
et cet accès restent à attester personnellement.

## Contrat de la procédure ponctuelle

`scripts/delivery_prerequisites.py` est indépendant des anciens lanceurs : il ne
change pas leurs restrictions synthétiques. `backup` exige un CID source complet,
un system_id exact, PostgreSQL 16, schema 0010, une app arrêtée sans OOM/exit anormal,
son image attendue, un kit source non vide et ses empreintes exactes. Transactions
préparées ou traitements SQL actifs refusés. Destination neuve exclusive.

Le dump est custom, lisibilité vérifiée par pg_restore. Des flux SQL triés sur fichier
permettent les empreintes/dénombrements par table : IDs, dates, JSON, marqueurs et
relations sont comparés, sans charger une table dans la mémoire Python. Colonnes,
contraintes, index, enums et positions de séquences sont attestés séparément. Source
empreintée avant/après dump : toute modification constatée refuse la copie. pg_dump
porte son propre instantané ; snapshot_bound est une borne UTC **avant** les relevés
et le dump, jamais la fin du transfert. Les checksums sont des preuves d’intégrité,
pas une reconstruction d’effets Telegram.

Le bundle contient le dump, un manifeste chiffré et le kit source compatible (code,
prompts nécessaires, migrations 0010, dépendances, Dockerfile et Compose vérifiés).
L’outil actuel est archivé séparément dans tools et empreinté ; ne pas exécuter les
anciens scripts archivés comme s’ils étaient la procédure approuvée. Aucun .env,
trousseau, export personnel ou working tree arbitraire dans ce kit.

GnuPG chiffre sur le serveur avec la clé publique exclusivement. Le bundle, dump et
flux SQL sont des fichiers privés temporaires ; pas de lecture intégrale en mémoire.
Le résultat contient `backup.tar.gpg`, `copy.json` (taille, SHA256, borne) et COMPLETE.
Un échec laisse une destination incomplète sans COMPLETE, ne touche aucune ancienne
copie et ne prétend jamais publication indépendante. Récupération initiée depuis le
Mac ; le serveur n’obtient aucun accès permanent au Mac.

`restore-check` exige une cible vide portant le label `botradar.recovery=0010-isolated`,
CID/system_id nouveaux, port `127.0.0.1:55530`, tmpfs, aucun volume existant, PostgreSQL
16. Refus des objets non système déjà présents. Vérification du kit et du dump avant
restauration, puis des données et du schéma avant toute mutation. Pas de quarantaine
ni démarrage d’app. Propriétaires et ACL de la cible d’exercice sont volontairement
ignorés (`--no-owner --no-acl`) : cette recette ne valide pas les droits opérationnels.
L’archive conserve les ACL source ; rôles/configuration réelle restent un volet séparé.
Les fichiers en clair créés par restore-check sont supprimés dans un finally. Le
bundle déchiffré et le PostgreSQL tmpfs doivent également être détruits par le pilote,
par leurs identités exactes, y compris sur échec. Pas de promesse d’effacement forensique.

## Ressources et recette synthétique

Serveur : environ 493 Mo de base, 30 Go de disque libre et 6,6 Go de RAM disponible
lors de la sonde ; Mac 16 Gio, Docker environ 3,8 Gio. Recontrôler avant GO. Prévoir
sur serveur au moins trois fois la taille estimée du dump plus les fichiers de tri,
et sur Mac la copie chiffrée, le bundle déchiffré temporaire et le dump extrait ;
PostgreSQL tmpfs dispose du budget mémoire Docker. Arrêter avant OOM/espace insuffisant,
sans augmenter arbitrairement les limites. Un tri SQL peut utiliser du temporaire
PostgreSQL : source arrêtée ne signifie pas absence de toute I/O de maintenance.

```sh
.venv/bin/python -B scripts/verify_consolidation.py unit --target tests/unit/test_delivery_prerequisites.py
.venv/bin/python -B scripts/exercise_delivery_0010.py --typecheck
.venv/bin/python -B scripts/exercise_delivery_0010.py
.venv/bin/ruff check .
```

La recette crée deux PostgreSQL neufs aux ports 55531/55530 depuis l’image locale
`sha256:c05eced0bdb41ea9b95a656472a6aa4d50cad0d8a2e33d14eb1c53fd6204f2ae`, avec
barrières dotenv/transports avant imports. Elle migre réellement à 0010 et réutilise
les données synthétiques de conservation déjà établies (liens, dates, notifié/pending,
préparation, anciens prix). Une ressource jamais démarrée matérialise le contrôle
d’arrêt de l’app ; aucun job/app/scheduler exécuté. Clés jetables uniquement, aucune
clé réelle utilisée. Copie locale simulée, jamais preuve d’un stockage indépendant réel.

## Ordre de phase B et commandes prévues

Réserver **deux heures d’intervention et quatre heures de disponibilité**. Aucun horaire
de début automatique : il vient avec le GO. Préparer UUID, fingerprint public et
SHA de merge à partir des attestations, sans supposer leurs valeurs. Les variables
ci-dessous ne sont pas des destinations par défaut : refuser une valeur absente.

1. Recontrôler tête/CI/prérequis, puis second passage local sous isolation.
   Merger en squash avec `gh pr merge 95 --repo ZeAmine/bot-radar --squash --match-head-commit "$PR_SHA"`.
   Fetch et résoudre SHA_MERGE via la PR. `git diff --exit-code "$PR_SHA" "$SHA_MERGE"`.
   `git switch --detach "$SHA_MERGE"` : main et la branche publiée conservent les commits
   validés ; ne pas reset le main local, un squash n’en est pas un descendant.

2. Après GO seulement, déposer le **seul outil autonome** et l’attestation source
   dans staging/<UUID> hors runtime. Vérifier leurs empreintes après transfert.
   Cela n’est ni synchronisation de l’app ni sauvegarde de données.

   Bloc exécutable sous **bash** (la substitution `${VPS_SSH_KEY//…}` n’est pas POSIX).

```sh
VPS_SSH_KEY="${VPS_SSH_KEY:?définir VPS_SSH_KEY}"
qkey="\"${VPS_SSH_KEY//\"/\"\"}\""
SSH="ssh -o StrictHostKeyChecking=yes -i $qkey"
# UUID créé une seule fois ; dossier serveur neuf, 0700, aucun écrasement.
rsync -av -e "$SSH" scripts/delivery_prerequisites.py \
  /Users/aminezegmou/BotRadar-recovery/procedures/source-0010.json \
  ubuntu@${VPS_HOST:?définir VPS_HOST}:/home/ubuntu/bot-radar-recovery/staging/$UUID/
```

3. Maintenance Radar seule. Fichier gateway actuel SHA256
   `91542ab8fa6e04610bc7196a743727620f4a1752e55d00d2cef706b2c535114d`,
   à recontrôler ; sauvegarder l’original privé sous staging/<UUID>/, puis préparer
   le candidat sans modifier le fichier actif. Le candidat conserve tous les autres
   octets, dont Stream. Valider avant reload. **Commandes serveur après GO** :

```sh
set -euo pipefail
umask 077
stage=/home/ubuntu/bot-radar-recovery/staging/$UUID
sha256sum -c <<'EXPECTED'
91542ab8fa6e04610bc7196a743727620f4a1752e55d00d2cef706b2c535114d  /home/ubuntu/gateway/Caddyfile
EXPECTED
cp -n /home/ubuntu/gateway/Caddyfile "$stage/Caddyfile.original"
python3 "$stage/delivery_prerequisites.py" maintenance \
  --file /home/ubuntu/gateway/Caddyfile --destination "$stage/Caddyfile.maintenance"
docker cp "$stage/Caddyfile.maintenance" caddy_gateway:/tmp/radar-maintenance-Caddyfile
docker exec caddy_gateway caddy validate --config /tmp/radar-maintenance-Caddyfile --adapter caddyfile
# Refuser une modification concurrente du gateway depuis la préparation.
cmp -s "$stage/Caddyfile.original" /home/ubuntu/gateway/Caddyfile
# Copier sur le même inode : un bind mount fichier ne suit pas os.replace.
cat "$stage/Caddyfile.maintenance" > /home/ubuntu/gateway/Caddyfile
docker exec caddy_gateway caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
# Vérifier Radar 503 et Stream inchangé avant arrêt ; si reload/contrôle échoue, stop du lot.
docker stop --time 120 5f3896fb62cfbbc1abee368b635803e10ae5cfa285a8280c38225ae4c9a4425a
# Vérifier Running=false, ExitCode=0, OOMKilled=false et zéro traitement SQL/préparé actif.
```

Le fichier Caddyfile actif ne doit pas être modifié si son hash a changé. Le temporaire
dans Caddy est propre à ce lot : retirer ce seul fichier après usage. Ne pas redémarrer
Caddy ou Stream ; ne pas docker compose down, ni supprimer un volume.

4. Dump final de la source arrêtée, vérification, chiffrement. `FINGERPRINT` doit être
   l’empreinte publique attestée, et key_home ne contenir aucune clé secrète :

```sh
python3 "$stage/delivery_prerequisites.py" backup \
  --socket unix:///var/run/docker.sock \
  --docker-config /home/ubuntu/bot-radar-recovery/docker-client \
  --container 72d9a4d805919682ddfe053c2b1fd9eea0f482b1f6005943a911129b23957935 \
  --system-id 7683840515455610913 \
  --app 5f3896fb62cfbbc1abee368b635803e10ae5cfa285a8280c38225ae4c9a4425a \
  --expected "$stage/source-0010.json" --root /home/ubuntu/bot-radar \
  --destination "$stage/archive" \
  --key-home /home/ubuntu/bot-radar-recovery/public-gnupg --fingerprint "$FINGERPRINT"
```

Récupération depuis Mac, destination neuve `backups/$UUID`, sans clé privée serveur :

```sh
rsync -av --ignore-existing -e "$SSH" \
  ubuntu@${VPS_HOST:?définir VPS_HOST}:/home/ubuntu/bot-radar-recovery/staging/$UUID/archive/ \
  /Users/aminezegmou/BotRadar-recovery/backups/$UUID/
.venv/bin/python -B scripts/delivery_prerequisites.py verify-copy \
  --file /Users/aminezegmou/BotRadar-recovery/backups/$UUID/backup.tar.gpg \
  --receipt /Users/aminezegmou/BotRadar-recovery/backups/$UUID/copy.json
```

Comparer aussi le SHA256 du reçu entre serveur et Mac (confiance dans la source SSH).
Déchiffrement dans un espace local 0700, hors tests, jamais en stdout/rapport :

```sh
gpg --no-options --homedir /Users/aminezegmou/BotRadar-recovery/private-gnupg-v2 \
  --no-auto-key-retrieve --auto-key-locate clear \
  --output "$PRIVATE_WORK/bundle.tar" --decrypt \
  /Users/aminezegmou/BotRadar-recovery/backups/$UUID/backup.tar.gpg
```

Cible créée neuve avec image déjà disponible, port libre vérifié avant création :

```sh
docker --host unix:///Users/aminezegmou/.docker/run/docker.sock --config "$EMPTY_DOCKER_CONFIG" \
  create --pull=never --label botradar.recovery=0010-isolated --label botradar.owner="$UUID" \
  --publish 127.0.0.1:55530:5432 --tmpfs /var/lib/postgresql/data:rw \
  --env POSTGRES_USER=synthetic --env POSTGRES_PASSWORD=synthetic_password \
  --env POSTGRES_DB=synthetic \
  sha256:c05eced0bdb41ea9b95a656472a6aa4d50cad0d8a2e33d14eb1c53fd6204f2ae
# Conserver CID exact ; start ce seul CID, puis lire pg_control_system() et major 16.
.venv/bin/python -B scripts/delivery_prerequisites.py restore-check \
  --socket unix:///Users/aminezegmou/.docker/run/docker.sock \
  --docker-config "$EMPTY_DOCKER_CONFIG" --container "$TARGET_CID" --system-id "$TARGET_SYSTEM_ID" \
  --bundle "$PRIVATE_WORK/bundle.tar" --work "$PRIVATE_WORK"
# Enfin : docker ... rm -f -v "$TARGET_CID" ; vérifier absence de ce CID.
# Supprimer uniquement PRIVATE_WORK créé pour cette vérification, même en échec.
```

Préserver archive chiffrée, reçu, manifeste chiffré inclus et clés. Pour conserver un
manifeste séparé, extraire ce seul membre vers un fichier privé temporaire puis le
chiffrer avec la clé publique avant de supprimer son clair. Aucun manifeste de données
réelles dans le dépôt ou les rapports. L’archive originale n’est jamais modifiée.

5. Configuration séparée, phase B uniquement : chiffrer un bundle privé contenant
   `/home/ubuntu/bot-radar/.env`, Compose, Caddyfile original, crontab et paramètres
   PostgreSQL nécessaires à la récupération. Propriétés/rôles doivent être documentés
   et conservés sous chiffrement ; ne pas afficher mots de passe ou exporter une clé
   SSH privée dans ce kit. Une copie chiffrée de configuration doit être récupérée et
   vérifiée séparément sur Mac ; elle n’entre pas dans le kit public ni les tests.
   Ne pas inclure les volumes Redis/FSM : ils ne prouvent pas une décision durable.
   Aucun de ces fichiers réels n’a été copié pendant la préparation.

```sh
# Serveur après GO, configurations explicitement choisies, pas de parcours de dossiers.
# Sauver crontab uniquement dans le staging privé, sans cat/log.
crontab -l > "$stage/crontab.original"
set -o pipefail
tar -C / -cf - home/ubuntu/bot-radar/.env home/ubuntu/bot-radar/compose.yml \
  "${stage#/}/Caddyfile.original" "${stage#/}/crontab.original" |
  gpg --no-options --homedir /home/ubuntu/bot-radar-recovery/public-gnupg \
    --batch --no-tty --no-auto-key-retrieve --auto-key-locate clear \
    --trust-model always --recipient "$FINGERPRINT" --encrypt \
    --output "$stage/configuration.tar.gpg"
# Nouvel output exclusif, SHA256/taille côté serveur/Mac, déchiffrement/lecture privée
# de vérification, puis suppression du clair ; aucune sortie de contenu dans les logs.
```

6. Seulement après restauration et configuration contrôlées : sync source finale et
   build sans démarrage. Réutiliser `deploy.step_sync` et `deploy.step_rebuild`
   individuellement avec contrôle de retour, jamais `make deploy`/run_steps complet
   (ils incluent notification Telegram). App doit rester arrêtée ; contrôler les
   empreintes hôte/image/runtime et les bind mounts avant migration.

```sh
cd /home/ubuntu/bot-radar
test "$(docker inspect --format '{{.State.Running}}' radar_app)" = false
docker compose run --rm --no-deps --pull never -T --entrypoint alembic app upgrade 0013_pause_actions < /dev/null
docker compose run --rm --no-deps --pull never -T --entrypoint alembic app current < /dev/null
```

Vérifier conservation et invariants (notifiés préservés, anciens pending unknown sans
reçu fabriqué, ancien historique intact, zéro action rétroactive). Si contrôle échoue,
pas de démarrage. Puis `deploy.step_start` et `deploy.step_verify`, sans step_notify.
Ces étapes importent seulement les outils de déploiement, pas le runtime applicatif
sur le Mac. Le nouveau SHA final est attesté, pas déduit du vieux marqueur.

7. Vérifier santé/SHA/empreintes/schéma/processus/scheduler, présence des secrets sans
   valeur, webhook sans token, logs filtrés et Stream mêmes identité/volumes/santé.
   Le démarrage normal configure Telegram et lance scheduler/poller : la maintenance
   HTTP n’est pas une suspension de ces tâches. Ne libérer aucun unknown/quarantaine.
   Santé ne prouve ni génération IA, ni réception Telegram, ni reprise des scans.

8. Retrait de maintenance : uniquement si Caddyfile actif égale exactement le candidat
   de ce lot et tous les contrôles sont verts. Valider original puis recopier sur le
   même inode et reload ; vérifier Radar/Stream. Écrire le SHA final dans le marqueur
   `.deployed_commit` sans notification Telegram. Recette manuelle à proposer, non
   exécutée sans autorisation spécifique.

## Cron et gestion des échecs

`backup_db.sh` refuse toujours l’usage opérationnel (code 2). Le cron existant
`0 4 * * *` écrit dans `backups/backup.log` sans surveillance indépendante. Après GO,
conserver le crontab original sous chiffrement, puis désactiver **cette seule ligne**
avec une mention `INACTIVE_BACKUP_INDEPENDENT_UNCONFIGURED`, sans effacer les autres
entrées ; contrôler le diff localement, sans afficher d’autres commandes/secrets.
Créer un statut explicite INACTIVE et le rapporter à Amine. Ne pas convertir son
refus en succès, ne pas utiliser `--synthetic-exercise` en cron, et ne pas annoncer
04:00/16:00 ou surveillance externe comme activées. La copie Mac est ponctuelle.
Toute adaptation réelle du cron attend GO et contrôle de l’identité de la ligne.
Commandes exactes après GO, original déjà protégé dans la configuration chiffrée :

```sh
crontab -l > "$stage/crontab.before-disable"
python3 "$stage/delivery_prerequisites.py" cron-candidate \
  --file "$stage/crontab.before-disable" --destination "$stage/crontab.inactive"
# Le générateur refuse zéro ou plusieurs lignes identifiées et ne change pas les autres.
crontab "$stage/crontab.inactive"
printf '%s\n' 'INACTIVE_BACKUP_INDEPENDENT_UNCONFIGURED — copie Mac ponctuelle uniquement' \
  > /home/ubuntu/bot-radar/backups/backup.status
# Comparer le crontab relu au candidat sans afficher ses commandes ; rapport à Amine.
```

Sauvegarde échouée avant migration : aucun sync/build/migration ; rester en maintenance
et préserver la copie incomplète. Option de sortie : reprise de l’image/code/config
anciens **seulement après** preuve de schéma 0010 inchangé, absence de traitement SQL
indéterminé et accord de reprise prévu par le GO ou donné après diagnostic. Aucun
ancien code relancé sur un arbre partiellement synchronisé.
Après début de modification du schéma : maintien en maintenance, pas d’ancien code,
downgrade, suppression de volumes ou restauration improvisée. Diagnostiquer la phase,
révision/empreintes et état réel sans corps sensible, proposer la récupération avant
opération non couverte. Une sauvegarde restaurée ne prouve pas l’absence d’effets
Telegram postérieurs ; réactivation métier/quarantaine réclament leur décision distincte.

## Limites et blocages à conserver

La recette synthétique vérifie le mécanisme, pas les 493 Mo réels ni le délai de
restauration. GPG assure confidentialité ; checksums reçus via SSH + source attestée
assurent le contrôle prévu, pas une preuve universelle contre une source compromise.
Pas de garantie exactement une fois ou de sauvegarde régulière indépendante.
Clé privée accessible et fingerprint serveur sont attestés ci-dessous. Seconde copie
physique et créneau de GO restent à confirmer. Aucun nettoyage ne supprime ces clés.

## Preuves du complément préparatoire

- 13 tests ciblés de schéma, maintenance, intégrité, attestation de cible et cron.
  Avant implémentation, collecte en erreur pour le module absent ; le test cron a
  ensuite reproduit son absence, puis les scénarios passent sans modifier leurs
  assertions pour masquer un échec.
- Sélection complète unitaires/performance : 1455 réussis, aucun ancien nodeid perdu,
  aucune désélection ou nouveau skip. Les 13 nouveaux cas portent le total attendu
  de la collection disjointe à 1781 avec les 326 scénarios d’intégration.
- Ruff dépôt propre ; app/diagnostics (167 fichiers), quatre anciens outils et deux
  nouveaux outils typés séparément sous isolation, aucune erreur.
- Recette finale PostgreSQL **160015** : source système 7695817541766107177, cible
  7695817546388721705. Backup synthétique 2,337 s ; restauration/contrôles 1,293 s.
  Ces temps ne dimensionnent ni les 493 Mo réels ni les deux heures de maintenance.
- Refus exercés : copie corrompue, destination existante, app active, cible non vide,
  identité différente, point de connexion incorrect et absence de clé privée.
- Source `18fbf9e55bae47abe960450ec295b4c66ccc1a403bdccaa8a96296015eb25444`, cible
  `28632d1a5e097ae56d86c5fdfd6d656f42095ccb6c06ea7e0077e14c5eee77d8`, témoin arrêté
  `6a1b6f56eb3a64f0892530fc67eab6d1421ac5db7d16c1176723c4fddf1d449a` : supprimés
  exactement, absence attestée. Toutes les recettes intermédiaires ont également
  nettoyé leurs seules ressources. Aucun trousseau réel supprimé.
- Barrières pré-import : une sonde dotenv, une sonde locale interdite et une sonde
  extérieure interdite volontairement ; aucune tentative accidentelle. Aucun test
  ordinaire, sauvegarde ou restauration n’a reçu de donnée réelle.

Pendant cette préparation, seuls les répertoires dédiés autorisés ont été créés sur
le serveur ; aucun fichier de runtime/configuration active ni cron modifié. Sur Mac,
répertoires 0700, script de saisie personnelle et attestation source de 202 fichiers
Git (prompts inclus), sans configuration réelle. Lors du complément initial
097c694, la clé réelle n’était pas encore créée ; cette situation est remplacée par
l’attestation ci-dessous. La seconde copie physique reste à effectuer avant GO.


## Attestation de la clé de récupération — 12 octobre 2026

Le premier trousseau `/Users/aminezegmou/BotRadar-recovery/private-gnupg/` contient
uniquement de l’initialisation ; il est conservé sans suppression ni réutilisation.
Le script initial de génération ne doit plus être réexécuté pour cette clé.

Trousseau effectif : `/Users/aminezegmou/BotRadar-recovery/private-gnupg-v2/`, 0700.
Empreinte publique primaire : `F4F552BC8787BE8E5AA12EA95AD4800AD25D5A43`.
La présence de la clé secrète correspondante est attestée ; aucun contenu privé
n’a été affiché ou exporté. Chiffrement d’un petit fichier exclusivement factice
et déchiffrement après saisie personnelle dans Pinentry : contenu identique relu.
Cela établit l’accès privé à cet instant, pas la seconde copie ou une restauration réelle.

Export **public uniquement** sur Mac et reçu/importé dans le trousseau public serveur
`/home/ubuntu/bot-radar-recovery/public-gnupg/`, 0700. Même empreinte primaire ;
zéro clé secrète listée sur le serveur. SHA256 de l’export public identique des deux côtés :
`e5233c11644fb83a0b3a4c04bc8fcd81df18c1cf6e95ea1c814b12ee3902a5fc`.
Le fichier public porte le nom `recovery-F4F552BC8787BE8E5AA12EA95AD4800AD25D5A43.asc`.
Pour les commandes de phase B, fixer explicitement
`FINGERPRINT=F4F552BC8787BE8E5AA12EA95AD4800AD25D5A43` après recontrôle ;
aucune phrase secrète dans les variables/arguments/logs.

Seconde copie sur support physique distinct : **non réalisée, non disponible**.
Support chiffré et point de montage doivent être fournis avant toute copie ; ne pas
confondre le premier trousseau et le nouveau sur le même Mac avec une seconde sauvegarde.
Aucun GO de phase B, merge, maintenance, sauvegarde réelle, migration ou déploiement.
