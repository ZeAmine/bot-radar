#!/usr/bin/env bash
# Script pour lancer le Vinted Poller Local sur votre Mac
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "============================================================"
echo "📡 Démarrage du Radar Bot - Poller Résidentiel Mac"
echo "============================================================"

if [ -f ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
elif command -v python3 &>/dev/null; then
    PYTHON_BIN="python3"
else
    echo "Erreur: Python introuvable."
    exit 1
fi

exec "$PYTHON_BIN" scripts/local_vinted_poller.py
