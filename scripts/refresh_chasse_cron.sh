#!/usr/bin/env bash

# Renouvellement autonome du jeton anti-bot Chasse-aux-livres (ADR-034/036) : si dernière observation certifiée < CHASSE_STALE_HOURS h, rien ; sinon POST /login, data-duih, validation série 1 an, injection API interne. Jamais de jeton sans validation.
# Identifiants : CHASSE_ACCOUNT_EMAIL/PASSWORD depuis env ou account.env (chmod 600, hors dépôt, jamais journalisé ; clés extraites sans exécuter le fichier).

# Incertitude (base injoignable, réponse illisible, compteur cassé) → renouvellement ; verrou anti-concurrence ; délai max ; journal tournant >1 Mo sans secret ; passe ratée alerte « Cotes de marché figées » sous 7 j.
# Jeton mort jusqu'à ~35 h ; baisser CHASSE_STALE_HOURS resserre au prix d'une connexion plus fréquente (filets 48 h et 7 j).

# Usage : ./scripts/refresh_chasse_cron.sh ; cron 0 2 * * * ; codes Python 0=validé, 1=validation/injection refusée, 2=capture/configuration impossible, 3=report (cron : sortie 0), 124=délai dépassé.
# Réglages : CHASSE_STALE_HOURS(24), CHASSE_HOME(~/chasse-refresh), CHASSE_VENV, CHASSE_CAPTURE_TIMEOUT(300), CHASSE_ACCOUNT_ENV, POSTGRES_CONTAINER/USER/DB.

set -euo pipefail

# PATH de cron minimal : fixé ici (date, docker).
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"

BASE_DIR="${CHASSE_HOME:-${HOME}/chasse-refresh}"
LOG_FILE="${BASE_DIR}/refresh.log"
LOCK_FILE="${BASE_DIR}/refresh.lock"
VENV="${CHASSE_VENV:-${BASE_DIR}/.venv}"
VENV_PYTHON="${VENV}/bin/python"
STALE_HOURS="${CHASSE_STALE_HOURS:-24}"
CAPTURE_TIMEOUT="${CHASSE_CAPTURE_TIMEOUT:-300}"

PG_CONTAINER="${POSTGRES_CONTAINER:-radar_postgres}"
PG_USER="${POSTGRES_USER:-radar_user}"
PG_DB="${POSTGRES_DB:-radar_db}"

mkdir -p "${BASE_DIR}"

log() {
    # Rotation simple : au-delà d'1 Mo, le courant devient .1.
    if [ -f "${LOG_FILE}" ] && [ "$(wc -c < "${LOG_FILE}" | tr -d ' ')" -gt 1048576 ]; then
        mv -f "${LOG_FILE}" "${LOG_FILE}.1"
    fi
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" >> "${LOG_FILE}"
}

# Verrou anti-concurrence ; sans flock (util-linux), on signale et continue sans verrou.
if command -v flock >/dev/null 2>&1; then
    exec 9>"${LOCK_FILE}"
    if ! flock -n 9; then
        log "Passe précédente encore en cours : abandon sans erreur."
        exit 0
    fi
else
    log "AVERTISSEMENT : flock absent (paquet util-linux) : passe lancée sans verrou."
fi

# Âge calculé par PostgreSQL (fuseaux compris) ; compteur vide/illisible vaut « vieux » (renouvellement). Seule lecture en base.
age_hours="$(
    docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${PG_DB}" -tAc \
        "SELECT COALESCE((EXTRACT(EPOCH FROM (now() - max(checked_at))) / 3600)::int, 9999)
           FROM market_price_snapshots
          WHERE raw_payload->>'historical_low_1y_cents' IS NOT NULL" 2>/dev/null || true
)"
age_hours="$(printf '%s' "${age_hours}" | tr -d '[:space:]')"
case "${age_hours}" in
    ''|*[!0-9]*)
        age_hours="9999"
        log "Âge de la dernière observation certifiée illisible : renouvellement par précaution."
        ;;
esac

if [ "${age_hours}" -le "${STALE_HOURS}" ]; then
    log "Observation certifiée de ${age_hours} h (seuil ${STALE_HOURS} h) : jeton encore neuf, rien à faire."
    exit 0
fi
log "Observation certifiée de ${age_hours} h (seuil ${STALE_HOURS} h) : renouvellement du jeton."

if [ ! -x "${VENV_PYTHON}" ]; then
    log "ERREUR : Python d'environnement absent (${VENV_PYTHON})."
    log "  python3 -m venv ${VENV} && ${VENV}/bin/pip install -e ${ROOT_DIR}"
    exit 2
fi

# Sans délai max, un Chromium pendu garderait le verrou ; refuser net.
if ! command -v timeout >/dev/null 2>&1; then
    log "ERREUR : commande timeout absente (paquet coreutils) : abandon pour ne pas risquer un verrou tenu à jamais."
    exit 2
fi

# INTERNAL_API_TOKEN depuis .env du dépôt si absent de l'env ; fichier non exécuté, clé jamais journalisée.
if [ -z "${INTERNAL_API_TOKEN:-}" ] && [ -f "${ROOT_DIR}/.env" ]; then
    env_line="$(grep -E '^INTERNAL_API_TOKEN=' "${ROOT_DIR}/.env" | head -n 1 || true)"
    INTERNAL_API_TOKEN="${env_line#INTERNAL_API_TOKEN=}"
    INTERNAL_API_TOKEN="${INTERNAL_API_TOKEN#\"}"
    INTERNAL_API_TOKEN="${INTERNAL_API_TOKEN%\"}"
    INTERNAL_API_TOKEN="${INTERNAL_API_TOKEN#\'}"
    INTERNAL_API_TOKEN="${INTERNAL_API_TOKEN%\'}"
    export INTERNAL_API_TOKEN
fi
if [ -z "${INTERNAL_API_TOKEN:-}" ]; then
    log "ERREUR : INTERNAL_API_TOKEN absent (variable ou ${ROOT_DIR}/.env) : injection impossible, abandon avant la capture."
    exit 2
fi

# Identifiants compte hors dépôt, fichier non exécuté : clés extraites comme .env, jamais journalisées ; env prime.
ACCOUNT_ENV="${CHASSE_ACCOUNT_ENV:-${BASE_DIR}/account.env}"
read_account_key() {
    local line
    line="$(grep -E "^$1=" "${ACCOUNT_ENV}" 2>/dev/null | head -n 1 || true)"
    line="${line#*=}"
    line="${line#\"}"; line="${line%\"}"; line="${line#\'}"; line="${line%\'}"
    printf '%s' "$line"
}
if [ -z "${CHASSE_ACCOUNT_EMAIL:-}" ] && [ -f "${ACCOUNT_ENV}" ]; then
    CHASSE_ACCOUNT_EMAIL="$(read_account_key CHASSE_ACCOUNT_EMAIL)"
fi
if [ -z "${CHASSE_ACCOUNT_PASSWORD:-}" ] && [ -f "${ACCOUNT_ENV}" ]; then
    CHASSE_ACCOUNT_PASSWORD="$(read_account_key CHASSE_ACCOUNT_PASSWORD)"
fi
if [ -z "${CHASSE_ACCOUNT_EMAIL:-}" ] || [ -z "${CHASSE_ACCOUNT_PASSWORD:-}" ]; then
    log "ERREUR : identifiants du compte Chasse absents (variables ou ${ACCOUNT_ENV}) : connexion impossible, abandon avant la tentative."
    log "  Creer ${ACCOUNT_ENV} (chmod 600, hors depot) avec CHASSE_ACCOUNT_EMAIL=… et CHASSE_ACCOUNT_PASSWORD=…"
    exit 2
fi
export CHASSE_ACCOUNT_EMAIL CHASSE_ACCOUNT_PASSWORD

# Sortie Python rejoint le journal ; seulement longueurs de jeton, jamais valeur ni identifiants.

rc=0
timeout -k 30 "${CAPTURE_TIMEOUT}" "${VENV_PYTHON}" "${SCRIPT_DIR}/refresh_chasse_token.py" \
    --api-url "http://127.0.0.1:8000" >> "${LOG_FILE}" 2>&1 || rc=$?

case "${rc}" in
    0)
        log "Terminé : jeton renouvelé et validé."
        ;;
    3)
        log "Report CAL (code 3) : nouvelle tentative à la prochaine échéance prévue."
        rc=0
        ;;
    124)
        log "Délai de ${CAPTURE_TIMEOUT} s dépassé : nouvelle tentative à la passe suivante."
        ;;
    *)
        log "Terminé en échec (code ${rc})."
        ;;
esac
exit "${rc}"
