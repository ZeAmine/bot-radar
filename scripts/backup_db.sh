#!/usr/bin/env bash
# ==============================================================================
# Script de sauvegarde automatique de la base PostgreSQL BotRadar
# Usage : ./scripts/backup_db.sh
# Crontab recommandée : 0 4 * * * /home/ubuntu/bot-radar/scripts/backup_db.sh >> /home/ubuntu/bot-radar/backups/backup.log 2>&1
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"
BACKUP_DIR="${ROOT_DIR}/backups"
TIMESTAMP="$(date +"%Y%m%d_%H%M%S")"
BACKUP_FILE="${BACKUP_DIR}/radar_db_${TIMESTAMP}.sql.gz"

mkdir -p "${BACKUP_DIR}"

echo "[$(date -u +"%Y-%m-%d %H:%M:%S UTC")] Début de la sauvegarde de radar_db..."

if docker ps --format '{{.Names}}' | grep -q "^radar_postgres$"; then
    docker exec radar_postgres pg_dump -U radar_user -d radar_db | gzip > "${BACKUP_FILE}"
    FILE_SIZE="$(du -sh "${BACKUP_FILE}" | cut -f1)"
    echo "[$(date -u +"%Y-%m-%d %H:%M:%S UTC")] Sauvegarde créée avec succès : ${BACKUP_FILE} (${FILE_SIZE})"
else
    echo "[$(date -u +"%Y-%m-%d %H:%M:%S UTC")] ERREUR : Le conteneur radar_postgres n'est pas actif." >&2
    exit 1
fi

# Rétention : suppression des sauvegardes de plus de 7 jours
find "${BACKUP_DIR}" -type f -name "radar_db_*.sql.gz" -mtime +7 -delete
echo "[$(date -u +"%Y-%m-%d %H:%M:%S UTC")] Nettoyage des sauvegardes > 7 jours effectué."
