#!/usr/bin/env bash
# Contrat : 0 4,16 * * * en UTC, sans activation ici ; aucun transport indépendant
# n'est configuré. L'exercice complet (dump custom, chiffrement) se lance explicitement.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" != "--synthetic-exercise" ]]; then
    echo "Sauvegarde indépendante non configurée : aucun succès annoncé." >&2
    exit 2
fi
shift
exec "${SCRIPT_DIR}/../.venv/bin/python" -B "${SCRIPT_DIR}/exercise_backup_recovery.py" "$@"
