#!/usr/bin/env bash
# ==============================================================================
# Renouvellement autonome du jeton anti-bot Chasse-aux-livres (ADR-033).
#
# Chemin, dans l'ordre :
#   1. Âge de la dernière observation certifiée en base. Si elle a moins de
#      CHASSE_STALE_HOURS heures, le jeton vit encore : rien à faire, le
#      navigateur ne se lance même pas.
#   2. Sinon : capture d'un jeton neuf (Chromium piloté par Playwright),
#      validation contre la série 1 an, injection via l'API interne. Jamais de
#      jeton poussé sans validation — un jeton « installé » qui ne rend rien
#      reprendrait le silence qu'on cherche à éviter.
#   3. Si la passe headless échoue et que `xvfb-run` est installé, seconde
#      passe en headful sous serveur X virtuel.
#
# Filets :
#   - Toute incertitude (base injoignable, réponse illisible, compteur cassé)
#     déclenche le renouvellement : un doute coûte une visite au site, jamais
#     une cote figée.
#   - Un verrou empêche deux passes simultanées ; un délai maximum sort un
#     Chromium pendu du chemin.
#   - Le journal tourne au-delà de 1 Mo. Ni le jeton, ni INTERNAL_API_TOKEN
#     n'y figurent jamais.
#   - Une passe ratée n'est pas muette : sans nouvelle observation certifiée,
#     l'alerte « Cotes de marché figées » revient sous 7 jours — c'est le
#     voyant d'échec du cron, en plus du journal.
#
# Rétention assumée : un jeton qui meurt peut rester mort jusqu'à ~35 h, le
# temps que l'âge dépasse le seuil au passage suivant (les filets d'alerte à
# 48 h et 7 j restent en place). Baisser CHASSE_STALE_HOURS resserre ce délai
# au prix d'une visite navigateur plus fréquente.
#
# Usage      : ./scripts/refresh_chasse_cron.sh
# Crontab    : 0 2 * * * /home/ubuntu/bot-radar/scripts/refresh_chasse_cron.sh
#              (heure du serveur, UTC : 02:00 UTC = 03:00/04:00 en France)
# Codes      : 0 = à jour ou renouvelé ; 1 = validation/injection refusée ;
#              2 = capture impossible ; 124 = délai dépassé.
# Réglages   : CHASSE_STALE_HOURS (24), CHASSE_HOME (~/chasse-refresh),
#              CHASSE_VENV (~/chasse-refresh/.venv), CHASSE_CAPTURE_TIMEOUT (300),
#              POSTGRES_CONTAINER/USER/DB (radar_postgres/radar_user/radar_db).
# ==============================================================================

set -euo pipefail

# Le PATH de cron est minimal : on le fixe, sinon `date` ou `docker` changent
# d'implémentation selon la machine.
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
    # Rotation simple : au-delà d'1 Mo, le courant devient .1. Un journal qui
    # grossit sans borne finit par devenir lui-même le défaut.
    if [ -f "${LOG_FILE}" ] && [ "$(wc -c < "${LOG_FILE}" | tr -d ' ')" -gt 1048576 ]; then
        mv -f "${LOG_FILE}" "${LOG_FILE}.1"
    fi
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" >> "${LOG_FILE}"
}

# --- 0. Une seule passe à la fois -------------------------------------------
# Un capture qui traîne ne doit pas être doublé par le passage suivant. Un
# `flock` absent ne doit surtout PAS se lire comme « verrou occupé » : ce serait
# un saut à chaque passage, muet jusqu'à l'alerte à 7 jours. On le signale et
# on continue sans verrou, quitte à doubler une passe lente.
if command -v flock >/dev/null 2>&1; then
    exec 9>"${LOCK_FILE}"
    if ! flock -n 9; then
        log "Passe précédente encore en cours : abandon sans erreur."
        exit 0
    fi
else
    log "AVERTISSEMENT : flock absent (paquet util-linux) : passe lancée sans verrou."
fi

# --- 1. Le jeton vit-il encore ? --------------------------------------------
# L'âge est calculé par PostgreSQL (fuseaux compris), pas par un `date` local.
# Un compteur vide ou illisible vaut « vieux » : on sait moins, donc on
# renouvelle. C'est la seule lecture de base du script.
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

# --- 2. Prérequis ------------------------------------------------------------
if [ ! -x "${VENV_PYTHON}" ]; then
    log "ERREUR : Python d'environnement absent (${VENV_PYTHON})."
    log "  python3 -m venv ${VENV} && ${VENV}/bin/pip install -e ${ROOT_DIR} playwright"
    log "  ${VENV}/bin/playwright install --with-deps chromium"
    exit 2
fi

# Sans délai maximum, un Chromium pendu garderait le verrou et bloquerait toutes
# les passes suivantes : mieux vaut refuser net que laisser un cadavre au poste.
if ! command -v timeout >/dev/null 2>&1; then
    log "ERREUR : commande timeout absente (paquet coreutils) : abandon pour ne pas risquer un verrou tenu à jamais."
    exit 2
fi

# Le secret d'injection vit dans .env du dépôt. On n'exécute pas ce fichier
# (une valeur exotique casserait tout le reste) : on en extrait la seule clé,
# sans jamais la journaliser.
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

# --- 3. Capture --------------------------------------------------------------
# La sortie du script Python rejoint le même journal ; il ne rapporte que des
# longueurs de jeton, jamais la valeur.
capture() {
    timeout -k 30 "${CAPTURE_TIMEOUT}" "$@" >> "${LOG_FILE}" 2>&1
}

rc=0
capture "${VENV_PYTHON}" "${SCRIPT_DIR}/refresh_chasse_token.py" \
    --api-url "http://127.0.0.1:8000" || rc=$?

case "${rc}" in
    0) ;;
    124)
        log "Délai de ${CAPTURE_TIMEOUT} s dépassé : pas de seconde tentative (Chromium peut traîner)."
        ;;
    *)
        if command -v xvfb-run >/dev/null 2>&1; then
            log "Passe headless en échec (code ${rc}) : nouvelle tentative en headful sous Xvfb."
            rc=0
            capture xvfb-run -a "${VENV_PYTHON}" "${SCRIPT_DIR}/refresh_chasse_token.py" \
                --api-url "http://127.0.0.1:8000" --headful || rc=$?
        else
            log "Passe headless en échec (code ${rc}) et xvfb-run absent :"
            log "  sudo apt-get install -y xvfb   (active la seconde tentative en headful)"
        fi
        ;;
esac

if [ "${rc}" -eq 0 ]; then
    log "Terminé : jeton renouvelé et validé."
else
    log "Terminé en échec (code ${rc})."
fi
exit "${rc}"
