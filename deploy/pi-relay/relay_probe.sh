#!/bin/sh
# Gabarit : sonde de santé du relais SOCKS5 local.
#
# Contrat, et c'est ce qui la rend sûre à planifier toutes les cinq minutes :
# - elle fait une poignée de main SOCKS5 locale et attend `05 00` ;
# - elle réessaie une fois, à quelques secondes d'intervalle, avant de conclure ;
# - en cas de double échec, elle **arrête** le relais pour que `flock` le relance ;
# - elle ne démarre **jamais** de relais : c'est le rôle du superviseur, qui
#   garantit l'unicité. Une sonde qui lancerait un relais créerait le doublon
#   qu'elle est censée prévenir.
#
# Le journal est minimal — un horodatage, un verdict, une action — sans adresse
# ni donnée de requête, et il est tronqué au-delà d'une taille bornée.
#
# Sortie : 0 si le relais répond (ou si le relais muet a été arrêté), 1 en cas
# d'erreur d'installation.

set -eu

: "${RELAY_DIR:?RELAY_DIR requis (répertoire du relais sur le Pi)}"
: "${RELAY_PORT:=8080}"
: "${PROBE_HOST:=127.0.0.1}"
: "${PROBE_MAX_LOG_BYTES:=65536}"
: "${PROBE_KEEP_LINES:=200}"

LOG="$RELAY_DIR/logs/probe.log"

probe() {
    /usr/bin/python3 - "$PROBE_HOST" "$RELAY_PORT" <<'PY'
import socket
import sys

hote, port = sys.argv[1], int(sys.argv[2])
try:
    with socket.create_connection((hote, port), timeout=3) as s:
        s.sendall(b"\x05\x01\x00")
        reply = s.recv(2)
except OSError:
    sys.exit(1)
sys.exit(0 if reply == b"\x05\x00" else 1)
PY
}

if probe; then
    exit 0
fi

sleep 3
if probe; then
    exit 0
fi

pids=$(/usr/bin/ss -H -ltnp "sport = :$RELAY_PORT" 2>/dev/null \
    | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u)
found=0
for pid in $pids; do
    /bin/kill -TERM "$pid" 2>/dev/null && found=1
done

stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if [ "$found" -eq 1 ]; then
    echo "$stamp sonde=ECHEC action=arret_relais" >> "$LOG"
else
    echo "$stamp sonde=ECHEC action=aucun_relais_a_arreter" >> "$LOG"
fi

if [ -f "$LOG" ]; then
    size=$(/usr/bin/stat -c %s "$LOG" 2>/dev/null || echo 0)
    if [ "$size" -gt "$PROBE_MAX_LOG_BYTES" ]; then
        tail -n "$PROBE_KEEP_LINES" "$LOG" > "$LOG.tmp" 2>/dev/null \
            && mv "$LOG.tmp" "$LOG"
    fi
fi
