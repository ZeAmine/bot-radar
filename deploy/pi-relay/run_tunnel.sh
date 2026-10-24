#!/bin/sh
# Gabarit : ouvre le tunnel SSH inverse qui expose le relais du Pi au VPS, en
# avant-plan, destiné à être appelé sous `flock` :
#   flock -n <verrou> run_tunnel.sh
#
# Options conservées telles quelles : `ExitOnForwardFailure=yes` ferme le tunnel
# plutôt que de le laisser ouvert sans redirection ; les `ServerAlive*` détectent
# une connexion morte ; `StrictHostKeyChecking=yes` n'accepte pas un hôte
# inconnu. `BatchMode=yes` interdit toute invite interactive sous `cron`.
#
# Paramètres : aucun n'a de valeur par défaut, pour qu'une installation
# incomplète échoue au démarrage au lieu de viser une adresse inventée.

set -eu

: "${RELAY_DIR:?RELAY_DIR requis (repertoire du relais sur le Pi)}"
: "${TUNNEL_KEY:?TUNNEL_KEY requis (cle privee dediee au tunnel)}"
: "${TUNNEL_REMOTE:?TUNNEL_REMOTE requis (compte@hote du VPS)}"
: "${TUNNEL_LISTEN_ADDRESS:?TUNNEL_LISTEN_ADDRESS requis (adresse ecoute cote VPS)}"
: "${RELAY_PORT:=8080}"
: "${TUNNEL_PORT:=1080}"

printf '%s tunnel_start\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$RELAY_DIR/logs/tunnel.log"

exec /usr/bin/ssh -N -T \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes \
    -o BatchMode=yes \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -i "$TUNNEL_KEY" \
    -R "$TUNNEL_LISTEN_ADDRESS:$TUNNEL_PORT:127.0.0.1:$RELAY_PORT" \
    "$TUNNEL_REMOTE" \
    >> "$RELAY_DIR/logs/tunnel.log" 2>&1
