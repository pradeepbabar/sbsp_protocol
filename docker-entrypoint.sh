#!/bin/sh
# Called by ContainerLab exec AFTER all veth links are attached.
# No polling needed — interfaces are already present.

ROUTER_ID="${SBSP_ROUTER_ID:-}"
AREA="${SBSP_AREA:-0.0.0.0}"
LOG_LEVEL="${SBSP_LOG_LEVEL:-INFO}"
export PYTHONPATH=/app

if [ -z "$ROUTER_ID" ]; then
    echo "[sbsp] ERROR: SBSP_ROUTER_ID not set"
    exit 1
fi

LAST_OCTET=$(echo "$ROUTER_ID" | cut -d. -f4)
IFACE_ARGS=""

echo "[sbsp] Configuring interfaces for router-id=$ROUTER_ID"

# Find all ethN interfaces where N >= 1
for iface in $(ip -o link show | awk -F': ' '{print $2}' | grep -E '^eth[1-9]' | sort); do
    NUM=$(echo "$iface" | tr -d 'eth')
    IP="10.${NUM}.${LAST_OCTET}.1"
    ip addr flush dev "$iface" 2>/dev/null || true
    ip addr add "${IP}/24" dev "$iface" 2>/dev/null || true
    ip link set "$iface" up 2>/dev/null || true
    echo "[sbsp]   $iface -> $IP/24"
    if [ -z "$IFACE_ARGS" ]; then
        IFACE_ARGS="${iface}:${IP}:10"
    else
        IFACE_ARGS="${IFACE_ARGS},${iface}:${IP}:10"
    fi
done

if [ -z "$IFACE_ARGS" ]; then
    echo "[sbsp] ERROR: No eth1+ interfaces found. Available:"
    ip -o link show | awk -F': ' '{print "  "$2}'
    exit 1
fi

echo "[sbsp] Starting: router-id=$ROUTER_ID interfaces=$IFACE_ARGS"
exec python -m sbsp.daemon.main \
    --router-id  "$ROUTER_ID" \
    --area       "$AREA" \
    --interfaces "$IFACE_ARGS" \
    --log-level  "$LOG_LEVEL"