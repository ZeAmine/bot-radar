#!/usr/bin/env bash
# Déploiement de production bot-radar : propriétaire unique de la séquence.
#
# La chaîne automatique (.github/workflows/deploy.yml) et le repli manuel
# (`make deploy`, ou ce script depuis un poste) appellent ce même fichier. Il
# n'existe pas de seconde copie de la séquence : une poussée sur main et un
# opérateur qui tape la commande font exactement la même chose.
#
# La séquence est celle qui a mis 265c80d en production le 31 octobre 2026,
# écrite à la main ce jour-là, réussie du premier coup et sans retour arrière :
#
#   1. relire le SHA que le serveur déclare déployé ;                (lecture)
#   2. ne rien faire si c'est déjà le SHA visé ;                     (idempotence)
#   3. relever l'identité des services dépendants ;                 (lecture)
#   4. refuser si une révision Alembic change, la base n'est jamais touchée ;
#   5. étiqueter l'image en marche comme filet de retour ;           (étiquette docker)
#   6. copier app/, alembic/, scripts/ et les fichiers racine AVEC suppression ;
#   7. construire l'image ;                                         (n'arrête rien)
#   8. recréer SEULEMENT le conteneur app ;                         (seule interruption)
#   9. attendre healthy, 180 s au plus ;
#  10. revenir en arrière UNIQUEMENT si l'application ne démarre pas ;
#  11. constater l'invariance des dépendances ;
#  12. écrire .deployed_commit sur le serveur, puis le relire.      (preuve lisible)
#
# Deux défauts mesurés le 31 octobre sont corrigés ici, pas ailleurs : la copie
# sans suppression laissait traîner des fichiers retirés du dépôt, et rien ne
# rendait visible l'écart entre le commit de main et celui du serveur.
#
# Ce que la chaîne ne touche jamais : .env, compose.yml, la base de données, les
# volumes, la cadence de scrutation, le proxy, les autres conteneurs du serveur.
#
# Codes de sortie, distincts parce qu'ils n'appellent pas la même correction :
#   0  déployé et sain, ou déjà à jour (rien n'a été refait)
#   1  paramètre manquant ou étape locale en échec (copie, construction)
#   2  refus : l'état de départ n'est pas celui attendu, rien n'a été modifié
#   3  retour arrière effectué : l'application n'a pas démarré
#   4  services dépendants non invariants, ou invariance non vérifiable
#   5  serveur injoignable, aucune action supplémentaire
#   6  la commande de retour arrière a échoué
#   7  déployé mais preuve invalide (.deployed_commit ou image en marche)

set -uo pipefail

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

usage() {
  cat <<'USAGE'
Usage : deploy/deploy.sh [--check | --dry-run | --help]

  (aucun argument)  déploie le SHA de HEAD
  --check           compare seulement le SHA visé et l'état du serveur, n'écrit rien
  --dry-run         énonce la séquence et les commandes, n'écrit rien
  --help            affiche cette aide

Paramètres (variables d'environnement, noms identiques aux secrets du dépôt) :
  DEPLOY_HOST         hôte du VPS                       (secret, obligatoire)
  DEPLOY_USER         utilisateur SSH                   (secret, obligatoire)
  DEPLOY_SSH_KEY      chemin de la clé privée           (secret, obligatoire)
  DEPLOY_KNOWN_HOSTS  empreinte d'hôte attendue         (secret, recommandé)
  DEPLOY_PORT         port SSH                          (défaut 22)
  DEPLOY_REMOTE_DIR   arbre de production               (défaut /home/ubuntu/bot-radar)
  DEPLOY_SHA          SHA visé                          (défaut : git rev-parse HEAD)
  DEPLOY_REPO         copie locale du dépôt             (défaut : parent de deploy/)
  DEPLOY_SCHEMA_ACK_TARGET  acquitte pour un seul SHA une migration déjà appliquée
                      par une opération autorisée séparée
  DEPLOY_HEALTH_TIMEOUT_SECONDS   attente de santé en secondes (défaut 180)
  DEPLOY_HEALTH_INTERVAL_SECONDS  intervalle des contrôles (défaut 5)
  DEPLOY_EVIDENCE     fichier de preuves clé=valeur, facultatif (la CI l'utilise)
USAGE
}

MODE=deploy
for arg in "$@"; do
  case "$arg" in
    --check) MODE=check ;;
    --dry-run) MODE=dry-run ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'argument inconnu : %s (voir --help)\n' "$arg" >&2; exit 2 ;;
  esac
done

REPO=${DEPLOY_REPO:-$(dirname -- "$SCRIPT_DIR")}
REMOTE_DIR=${DEPLOY_REMOTE_DIR:-/home/ubuntu/bot-radar}
PORT=${DEPLOY_PORT:-22}
HEALTH_TIMEOUT=${DEPLOY_HEALTH_TIMEOUT_SECONDS:-180}
HEALTH_INTERVAL=${DEPLOY_HEALTH_INTERVAL_SECONDS:-5}
APP_CONTAINER=radar_app
DEPENDENT_CONTAINERS="radar_postgres radar_redis"
ROLLBACK_TAG=""

say() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
fail() { say "ARRÊT : $*"; }
note() { printf '  - %s\n' "$*"; }

# Preuve clé=valeur : la CI en fait le résumé du passage, lisible sans se connecter.
evidence() {
  [ -n "${DEPLOY_EVIDENCE:-}" ] || return 0
  printf '%s=%s\n' "$1" "$2" >> "$DEPLOY_EVIDENCE"
}

# --- paramètres : un secret manquant est nommé, jamais deviné ------------------
MISSING=""
[ -n "${DEPLOY_HOST:-}" ] || MISSING="$MISSING DEPLOY_HOST"
[ -n "${DEPLOY_USER:-}" ] || MISSING="$MISSING DEPLOY_USER"
[ -n "${DEPLOY_SSH_KEY:-}" ] || MISSING="$MISSING DEPLOY_SSH_KEY"
if [ -n "$MISSING" ]; then
  fail "paramètre(s) de déploiement manquant(s) :$MISSING"
  for name in $MISSING; do
    note "$name : à poser comme secret de dépôt (gh secret set $name) ou dans l'environnement"
  done
  if [ "$MODE" = dry-run ]; then
    note "la séquence serait celle de --help ; rien n'a été exécuté"
    exit 0
  fi
  exit 1
fi
if [ ! -f "$DEPLOY_SSH_KEY" ]; then
  fail "DEPLOY_SSH_KEY ne désigne aucun fichier lisible : $DEPLOY_SSH_KEY"
  exit 1
fi
# Le chemin de la clé et l'arbre distant traversent `rsync -e` et des commandes
# distantes citées : une espace y deviendrait une coupure d'argument silencieuse.
for pair in "DEPLOY_SSH_KEY:$DEPLOY_SSH_KEY" "DEPLOY_REMOTE_DIR:$REMOTE_DIR"; do
  case "${pair#*:}" in
    *" "*) fail "${pair%%:*} contient une espace : transport impossible sans ambiguïté" ; exit 1 ;;
  esac
done

HOST_TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"
SSH_OPTS=(-o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o BatchMode=yes -i "$DEPLOY_SSH_KEY" -p "$PORT")

# Toute commande distante passe par ici : une seule définition du transport.
rmt() { ssh "${SSH_OPTS[@]}" "$HOST_TARGET" "$@"; }
need_server() {
  if rmt true >/dev/null 2>&1; then return 0; fi
  fail "serveur injoignable (${1:-vérification préalable}) : aucune action, production laissée telle quelle"
  note "hôte ${HOST_TARGET}:${PORT} ; une empreinte d'hôte absente se règle par le secret DEPLOY_KNOWN_HOSTS"
  exit 5
}

# Identité des services que le déploiement ne nomme jamais. `Id` et `StartedAt`
# sont les deux valeurs qu'une recréation ou un redémarrage ne peuvent pas
# préserver ; `absente` est rendu tel quel pour que l'absence se voie.
DEPS_SCRIPT='for c in radar_postgres radar_redis; do
  id=$(docker inspect -f "{{.Id}}" "$c" 2>/dev/null) || id=absente
  started=$(docker inspect -f "{{.State.StartedAt}}" "$c" 2>/dev/null) || started=absente
  running=$(docker inspect -f "{{.State.Running}}" "$c" 2>/dev/null) || running=absente
  printf "%s|%s|%s|%s\n" "$c" "$id" "$started" "$running"
done'
deps_capture() { rmt "$DEPS_SCRIPT" 2>/dev/null; }
deps_readable() {
  [ -n "$1" ] || return 1
  case "$1" in *absente*) return 1 ;; esac
  [ "$(printf '%s\n' "$1" | grep -c '|')" -ge 2 ] || return 1
  return 0
}

# --- préalables locaux : on ne déploie que ce qu'on a sous les yeux ------------
for tool in git rsync ssh base64; do
  if ! command -v "$tool" >/dev/null 2>&1; then fail "$tool est introuvable"; exit 1; fi
done

LOCAL_HEAD=$(git -C "$REPO" rev-parse HEAD 2>/dev/null)
TARGET=${DEPLOY_SHA:-$LOCAL_HEAD}
if [ -z "$LOCAL_HEAD" ] || [ -z "$TARGET" ]; then
  fail "SHA visé indéterminable (copie locale ${LOCAL_HEAD:-illisible}, visé ${TARGET:-vide})"
  exit 2
fi
say "SHA visé = $TARGET ; copie locale HEAD = $LOCAL_HEAD"
evidence target_sha "$TARGET"

if [ "$LOCAL_HEAD" != "$TARGET" ]; then
  fail "la copie locale n'est pas sur le SHA visé : rien copié, rien construit"
  exit 2
fi
if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
  fail "la copie locale n'est pas propre : rien copié, rien construit"
  exit 2
fi

# --- ce que la chaîne refuse d'envoyer : une révision Alembic non appliquée ---
# La chaîne n'applique aucune migration : écrire un schéma dans la base est une
# opération autorisée séparément. Elle refuse donc de servir du code qui attend
# une révision absente, plutôt que de laisser l'écart se découvrir en production.
# L'acquittement DEPLOY_SCHEMA_ACK_TARGET vaut pour un seul SHA, et n'a de sens
# qu'après une migration faite et vérifiée par une autre procédure.
schema_guard() {
  local deployed="$1"
  if ! git -C "$REPO" cat-file -e "${deployed}^{commit}" 2>/dev/null; then
    fail "le commit déployé $deployed est inconnu de la copie locale : écart non comparable"
    note "récupérer l'historique (git fetch origin) puis relancer ; rien n'a été modifié"
    return 2
  fi
  local changes
  changes=$(git -C "$REPO" diff --name-status "$deployed" "$TARGET" -- alembic/versions 2>/dev/null)
  if [ -z "$changes" ]; then return 0; fi
  if [ "${DEPLOY_SCHEMA_ACK_TARGET:-}" = "$TARGET" ]; then
    say "AVERTISSEMENT : révisions Alembic modifiées, acquittées pour $TARGET"
    while IFS= read -r line; do note "$line"; done <<< "$changes"
    return 0
  fi
  fail "des révisions Alembic changent entre $deployed et $TARGET ; la chaîne ne touche jamais la base"
  while IFS= read -r line; do note "$line"; done <<< "$changes"
  note "appliquer la migration par une opération autorisée séparée, puis relancer"
  note "si elle est déjà appliquée et vérifiée : DEPLOY_SCHEMA_ACK_TARGET=$TARGET"
  return 2
}

# --- mode --check : ne compare que le SHA et l'état du serveur ----------------
if [ "$MODE" = check ]; then
  need_server "contrôle (--check)"
  DEPLOYED=$(rmt "cat $REMOTE_DIR/.deployed_commit" 2>/dev/null)
  if [ -z "$DEPLOYED" ]; then
    say "le serveur ne déclare aucun commit déployé ($REMOTE_DIR/.deployed_commit illisible)"
    exit 1
  fi
  say "déployé sur le serveur = $DEPLOYED ; visé = $TARGET"
  if [ "$DEPLOYED" = "$TARGET" ]; then
    say "à jour : rien à déployer"
    exit 0
  fi
  say "EN RETARD : le serveur n'exécute pas le commit visé"
  git -C "$REPO" log --oneline "$DEPLOYED..$TARGET" 2>/dev/null | sed 's/^/  /' || true
  exit 1
fi

# --- mode --dry-run : énonce la séquence, n'écrit rien ------------------------
if [ "$MODE" = dry-run ]; then
  say "SIMULATION : aucune écriture, locale ou distante"
  note "1. contrôle de joignabilité du serveur ${HOST_TARGET}:${PORT}"
  note "2. relecture de $REMOTE_DIR/.deployed_commit"
  note "3. si déjà $TARGET : succès immédiat, ni construction ni redémarrage"
  note "4. relevé de l'identité de $DEPENDENT_CONTAINERS"
  note "5. refus si des révisions Alembic changent entre le commit déployé et $TARGET"
  note "6. étiquette de retour sur l'image en marche, puis vérification de son identité"
  note "7. rsync -rlptz --delete sur app/ alembic/ scripts/ ; fichiers racine sans suppression"
  note "8. docker compose build app (le conteneur en marche n'est pas arrêté)"
  note "9. docker compose up -d --no-build --pull never --no-deps --force-recreate app"
  note "10. attente healthy, ${HEALTH_TIMEOUT} s au plus"
  note "11. retour arrière seulement si l'application ne démarre pas"
  note "12. écriture de $REMOTE_DIR/.deployed_commit = $TARGET, puis relecture"
  need_server "simulation, lecture seule"
  DEPLOYED=$(rmt "cat $REMOTE_DIR/.deployed_commit" 2>/dev/null)
  if [ -z "$DEPLOYED" ]; then
    say "serveur : aucun commit déployé lisible ; un vrai passage refuserait de continuer"
  elif [ "$DEPLOYED" = "$TARGET" ]; then
    say "serveur : déjà sur $TARGET ; un vrai passage sortirait en succès sans rien refaire"
  else
    say "serveur : $DEPLOYED ; un vrai passage déploierait $TARGET"
    git -C "$REPO" log --oneline "$DEPLOYED..$TARGET" 2>/dev/null | sed 's/^/  /' || true
  fi
  exit 0
fi

# ============================ déploiement =====================================
need_server "vérification préalable"
say "serveur joignable (${HOST_TARGET}:${PORT})"

# 1. L'état de départ : ce que le serveur déclare, pas ce qu'on suppose.
DEPLOYED=$(rmt "cat $REMOTE_DIR/.deployed_commit" 2>/dev/null)
if [ -z "$DEPLOYED" ]; then
  fail "le serveur ne déclare aucun commit déployé : $REMOTE_DIR/.deployed_commit illisible ou vide"
  note "rien n'a été copié, construit ni redémarré ; l'écart n'est pas comparable"
  exit 2
fi
say "commit déployé déclaré par le serveur = $DEPLOYED"
evidence deployed_before_sha "$DEPLOYED"

# 2. Idempotence : le SHA visé tourne déjà, on ne reconstruit ni ne redémarre.
if [ "$DEPLOYED" = "$TARGET" ]; then
  say "déjà à jour : $TARGET est le commit déployé ; ni construction ni redémarrage"
  evidence outcome already-deployed
  exit 0
fi

RUNNING_IMAGE=$(rmt "docker inspect -f '{{.Image}}' $APP_CONTAINER" 2>/dev/null)
if [ -z "$RUNNING_IMAGE" ]; then
  fail "image du conteneur $APP_CONTAINER illisible : filet de retour impossible à établir"
  note "rien n'a été copié ni redémarré"
  exit 2
fi
say "image en marche avant opération = $RUNNING_IMAGE"
evidence previous_image "$RUNNING_IMAGE"

# 3. L'identité des dépendances, relevée avant toute mutation.
DEPENDENTS_BEFORE=$(deps_capture)
if ! deps_readable "$DEPENDENTS_BEFORE"; then
  fail "identité de $DEPENDENT_CONTAINERS illisible : invariance non vérifiable, rien n'est touché"
  note "ne pas avoir pu regarder ne vaut pas un feu vert"
  exit 4
fi
say "dépendances relevées avant opération :"
printf '%s\n' "$DEPENDENTS_BEFORE" | sed 's/^/    /'

# 4. Révisions Alembic : refus avant toute écriture si le schéma bouge.
schema_guard "$DEPLOYED" || exit $?

# 5. Filet de retour : étiqueter l'image en marche, et vérifier le tag.
ROLLBACK_TAG="bot-radar-app:rollback-$(printf '%s' "$TARGET" | cut -c1-7)-$(date -u +%Y%m%dT%H%M%SZ)"
say "pose du filet : $ROLLBACK_TAG"
if ! rmt "docker tag bot-radar-app:latest $ROLLBACK_TAG"; then
  fail "étiquetage de retour impossible : rien n'a été copié ni redémarré"
  exit 2
fi
ROLLBACK_ID=$(rmt "docker image inspect -f '{{.Id}}' $ROLLBACK_TAG" 2>/dev/null)
if [ "$ROLLBACK_ID" != "$RUNNING_IMAGE" ]; then
  fail "le filet ne pointe pas sur l'image en marche (${ROLLBACK_ID:-aucun}) : déploiement refusé"
  note "rien n'a été copié ni redémarré"
  exit 2
fi
say "filet vérifié : $ROLLBACK_TAG = $RUNNING_IMAGE"
evidence rollback_tag "$ROLLBACK_TAG"

# 6. Copie des sources AVEC suppression : un fichier retiré du dépôt disparaît
#    du serveur. Les fichiers racine sont copiés sans --delete, et .env comme
#    compose.yml ne sont jamais dans la liste : la configuration reste au serveur.
RSYNC_SSH="ssh ${SSH_OPTS[*]}"
copy_sources() {
  local path
  for path in app alembic scripts; do
    say "copie de $path/ (avec suppression)"
    rsync -rlptz --delete --exclude '__pycache__' -e "$RSYNC_SSH" \
      "$REPO/$path/" "$HOST_TARGET:$REMOTE_DIR/$path/" || return 1
  done
  say "copie des fichiers racine (sans suppression)"
  rsync -rlptz -e "$RSYNC_SSH" \
    "$REPO/alembic.ini" "$REPO/pyproject.toml" "$REPO/requirements.lock" \
    "$HOST_TARGET:$REMOTE_DIR/" || return 1
  return 0
}
if ! copy_sources; then
  fail "copie interrompue : le conteneur n'a pas été touché, l'ancienne version sert toujours"
  exit 1
fi
say "sources copiées"

# 7. Construction : le conteneur en marche n'est pas arrêté.
say "construction de l'image"
if ! rmt "cd $REMOTE_DIR && docker compose build app"; then
  fail "construction en échec : le conteneur n'a pas été touché, l'ancienne version sert toujours"
  exit 1
fi
BUILT_IMAGE=$(rmt "docker image inspect -f '{{.Id}}' bot-radar-app:latest" 2>/dev/null)
if [ -z "$BUILT_IMAGE" ]; then
  fail "image construite illisible : conteneur non recréé"
  exit 1
fi
say "image construite = $BUILT_IMAGE"
evidence built_image "$BUILT_IMAGE"

# La construction vient de réécrire `bot-radar-app:latest` en place : c'est le
# moment où le filet doit encore être atteignable. Un tag perdu se découvrirait
# sinon au moment du retour arrière, c'est-à-dire trop tard.
ROLLBACK_AFTER_BUILD=$(rmt "docker image inspect -f '{{.Id}}' $ROLLBACK_TAG" 2>/dev/null)
if [ "$ROLLBACK_AFTER_BUILD" != "$RUNNING_IMAGE" ]; then
  fail "le filet a disparu pendant la construction (${ROLLBACK_AFTER_BUILD:-aucun} au lieu de $RUNNING_IMAGE) : conteneur non recréé"
  exit 2
fi
say "filet toujours en place après construction"

# 8. Recréation du SEUL conteneur app. --no-deps empêche Compose de réconcilier
#    postgres ou redis (c'est ce qui les avait recréés le 16 octobre 2026) ;
#    --no-build et --pull never garantissent que l'image servie est celle qui
#    vient d'être construite, pas une image tirée du réseau.
say "recréation du seul conteneur $APP_CONTAINER"
if ! rmt "cd $REMOTE_DIR && docker compose up -d --no-build --pull never --no-deps --force-recreate app"; then
  say "AVERTISSEMENT : la recréation a rendu une erreur ; l'état du conteneur est incertain"
fi

# 9. Attente de santé. Un contrôle qui reste bloqué n'est pas un feu vert.
say "attente de santé (${HEALTH_TIMEOUT} s au plus, contrôle toutes les ${HEALTH_INTERVAL} s)"
ATTEMPTS=$((HEALTH_TIMEOUT / HEALTH_INTERVAL))
[ "$ATTEMPTS" -ge 1 ] || ATTEMPTS=1
HEALTH=inconnue
ATTEMPT=0
while [ "$ATTEMPT" -lt "$ATTEMPTS" ]; do
  ATTEMPT=$((ATTEMPT + 1))
  sleep "$HEALTH_INTERVAL"
  if ! rmt true >/dev/null 2>&1; then
    fail "serveur injoignable pendant l'attente de santé (tentative ${ATTEMPT}/${ATTEMPTS}) : aucune action supplémentaire"
    exit 5
  fi
  HEALTH=$(rmt "docker inspect -f '{{.State.Health.Status}}' $APP_CONTAINER" 2>/dev/null)
  RUNNING=$(rmt "docker inspect -f '{{.State.Running}}' $APP_CONTAINER" 2>/dev/null)
  say "  ${ATTEMPT}/${ATTEMPTS} : running=${RUNNING:-inconnu} health=${HEALTH:-inconnue}"
  if [ "$RUNNING" = "true" ] && [ "$HEALTH" = "healthy" ]; then break; fi
done
evidence health "$HEALTH"

# 10. Retour arrière : seule condition autorisée, l'application ne démarre pas.
if [ "$HEALTH" != "healthy" ]; then
  say "l'application ne démarre pas (santé=${HEALTH:-inconnue}) : retour arrière"
  evidence outcome rolled-back
  if ! rmt "cd $REMOTE_DIR && docker tag $ROLLBACK_TAG bot-radar-app:latest && docker compose up -d --no-build --pull never --no-deps --force-recreate app"; then
    fail "la commande de retour arrière a échoué : aucune autre action, examiner le serveur"
    exit 6
  fi
  RB_ATTEMPT=0
  RB_HEALTH=inconnue
  while [ "$RB_ATTEMPT" -lt "$ATTEMPTS" ]; do
    RB_ATTEMPT=$((RB_ATTEMPT + 1))
    sleep "$HEALTH_INTERVAL"
    RB_HEALTH=$(rmt "docker inspect -f '{{.State.Health.Status}}' $APP_CONTAINER" 2>/dev/null)
    [ "$RB_HEALTH" = healthy ] && break
  done
  RB_IMAGE=$(rmt "docker inspect -f '{{.Image}}' $APP_CONTAINER" 2>/dev/null)
  say "après retour : santé=${RB_HEALTH:-inconnue} image=${RB_IMAGE:-inconnue}"
  if [ "$RB_IMAGE" = "$RUNNING_IMAGE" ]; then
    rmt "printf '%s\n' $DEPLOYED > $REMOTE_DIR/.deployed_commit"
    say ".deployed_commit laissé à $DEPLOYED"
  else
    say "l'image restaurée n'est pas celle d'avant : .deployed_commit laissé tel quel"
  fi
  fail "déploiement de $TARGET non démarré, retour arrière effectué"
  exit 3
fi
say "application saine"

# 11. Invariance des dépendances : constatée, pas supposée.
DEPENDENTS_AFTER=$(deps_capture)
if ! deps_readable "$DEPENDENTS_AFTER"; then
  fail "identité de $DEPENDENT_CONTAINERS illisible après démarrage : invariance non vérifiée"
  note "ni modifiées ni intactes n'est établi ; aucune réparation, aucun retour arrière"
  exit 4
fi
if [ "$DEPENDENTS_BEFORE" != "$DEPENDENTS_AFTER" ]; then
  fail "un service dépendant a changé pendant le déploiement applicatif"
  printf '%s\n' "$DEPENDENTS_BEFORE" | sed 's/^/    avant: /'
  printf '%s\n' "$DEPENDENTS_AFTER" | sed 's/^/    après: /'
  note "aucune réparation automatique ; modifier une dépendance est une opération séparée"
  exit 4
fi
say "dépendances inchangées ($DEPENDENT_CONTAINERS)"

# 12. La preuve : .deployed_commit écrit ET relu, image en marche = image construite.
if ! rmt "printf '%s\n' $TARGET > $REMOTE_DIR/.deployed_commit"; then
  fail "$REMOTE_DIR/.deployed_commit n'a pas pu être écrit : le SHA déployé reste illisible"
  exit 7
fi
COMMIT_READ=$(rmt "cat $REMOTE_DIR/.deployed_commit" 2>/dev/null)
SERVING_IMAGE=$(rmt "docker inspect -f '{{.Image}}' $APP_CONTAINER" 2>/dev/null)
say "preuve : image en marche=${SERVING_IMAGE:-inconnu} attendue=$BUILT_IMAGE ; commit relu=${COMMIT_READ:-inconnu}"
evidence deployed_after_sha "${COMMIT_READ:-inconnu}"
if [ "$COMMIT_READ" != "$TARGET" ] || [ "$SERVING_IMAGE" != "$BUILT_IMAGE" ]; then
  fail "preuve invalide : le SHA relu ou l'image en marche ne correspond pas à ce qui a été construit"
  exit 7
fi

# 13. Notification de mise en ligne : au mieux, jamais bloquante, hors du chemin
#     critique. Elle ne change ni le code ni l'état du serveur.
say "notification de mise en ligne (au mieux)"
NOTIFY_TEXT="<b>Mise en ligne effectuée</b>
${DEPLOYED:0:7} → ${TARGET:0:7}"
NOTIFY_B64=$(printf '%s' "$NOTIFY_TEXT" | base64 | tr -d '\n')
rmt "cd $REMOTE_DIR && docker compose exec -T app python scripts/notify_deploy.py --base64 '$NOTIFY_B64'" \
  >/dev/null 2>&1 || say "AVERTISSEMENT : notification non envoyée (sans effet sur le déploiement)"

say "=== déploiement terminé : $TARGET est en production et sain"
evidence outcome deployed
evidence outcome_sha "$TARGET"
exit 0
