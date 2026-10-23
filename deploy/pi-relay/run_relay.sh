#!/bin/sh
# Gabarit : lance le relais SOCKS5 en avant-plan. Aucune valeur propre à une
# installation : celles qui le sont sont à remplacer avant déploiement, ou sont
# lues depuis l'environnement.
#
# Ce script est destiné à être appelé sous `flock`, jamais directement :
#   flock -n <verrou> run_relay.sh
# `exec` remplace le shell par le relais, donc le verrou tenu par `flock` reste
# pris aussi longtemps que le relais vit. Une invocation concurrente échoue au
# verrou au lieu de démarrer une seconde instance.

set -eu

: "${RELAY_DIR:?RELAY_DIR requis (répertoire du relais sur le Pi)}"
: "${RELAY_PORT:=8080}"

exec /usr/bin/python3 "$RELAY_DIR/leboncoin_residential_relay.py" \
    --port "$RELAY_PORT" \
    >> "$RELAY_DIR/logs/relay.log" 2>&1
