#!/usr/bin/env bash
# Contrat proposé : 0 4,16 * * * en UTC ; aucune activation dans ce lot.
# Le dépôt ne dispose d'aucun transport indépendant configuré.
# L'exercice complet, avec dump custom et chiffrement, est lancé explicitement.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" != "--synthetic-exercise" ]]; then
    echo "Sauvegarde indépendante non configurée : aucun succès annoncé." >&2
    exit 2
fi
shift
exec "${SCRIPT_DIR}/../.venv/bin/python" -B "${SCRIPT_DIR}/exercise_backup_recovery.py" "$@"
