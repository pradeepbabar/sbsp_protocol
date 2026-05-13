#!/bin/sh
# docker-entrypoint.sh
# Called by ContainerLab exec AFTER all veth links are attached.

ROUTER_ID="${SBSP_ROUTER_ID:-}"
AREA="${SBSP_AREA:-0.0.0.0}"
LOG_LEVEL="${SBSP_LOG_LEVEL:-INFO}"
LOOPBACK="${SBSP_LOOPBACK:-}"
export PYTHONPATH=/app

if [ -z "$ROUTER_ID" ]; then
    echo "[sbsp] ERROR: SBSP_ROUTER_ID not set"; exit 1
fi

LAST_OCTET=$(echo "$ROUTER_ID" | cut -d. -f4)

echo "[sbsp] Configuring router-id=$ROUTER_ID loopback=$LOOPBACK"

# ------------------------------------------------------------------
# Create loopback interface with the router's own subnet
# e.g. SBSP_LOOPBACK=192.168.1.0/24 -> lo:1 gets 192.168.1.1/24
# ------------------------------------------------------------------
if [ -n "$LOOPBACK" ]; then
    LOOPBACK_NET=$(echo "$LOOPBACK" | cut -d/ -f1)
    LOOPBACK_MASK=$(echo "$LOOPBACK" | cut -d/ -f2)
    # Host address = network address with last octet = 1
    LOOPBACK_IP=$(echo "$LOOPBACK_NET" | awk -F. '{print $1"."$2"."$3".1"}')

    # Create a dedicated loopback alias (lo:1)
    ip link add name lo1 type dummy 2>/dev/null || true
    ip link set lo1 up 2>/dev/null || true
    ip addr flush dev lo1 2>/dev/null || true
    ip addr add "${LOOPBACK_IP}/${LOOPBACK_MASK}" dev lo1 2>/dev/null || true
    echo "[sbsp]   lo1 -> ${LOOPBACK_IP}/${LOOPBACK_MASK} (loopback subnet)"
fi

# ------------------------------------------------------------------
# Configure data-plane interfaces (eth1, eth2, ...)
# eth1 -> 10.1.<LAST_OCTET>.1/24
# eth2 -> 10.2.<LAST_OCTET>.1/24
# ------------------------------------------------------------------
IFACE_ARGS=""

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
    echo "[sbsp] ERROR: No eth1+ interfaces found"; exit 1
fi

echo "[sbsp] Starting daemon..."
exec python -m sbsp.daemon.main \
    --router-id  "$ROUTER_ID" \
    --area       "$AREA" \
    --interfaces "$IFACE_ARGS" \
    --loopback   "${LOOPBACK:-}" \
    --log-level  "$LOG_LEVEL"