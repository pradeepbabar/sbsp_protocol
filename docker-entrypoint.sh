#!/bin/sh
# docker-entrypoint.sh
# Auto-configures SBSP daemon from environment variables set by ContainerLab.
#
# Environment variables:
#   SBSP_ROUTER_ID   - e.g. "10.0.0.1"         (required)
#   SBSP_AREA        - e.g. "0.0.0.0"           (default: 0.0.0.0)
#   SBSP_LOG_LEVEL   - INFO / DEBUG / WARNING    (default: INFO)

set -e

ROUTER_ID="${SBSP_ROUTER_ID:-}"
AREA="${SBSP_AREA:-0.0.0.0}"
LOG_LEVEL="${SBSP_LOG_LEVEL:-INFO}"

if [ -z "$ROUTER_ID" ]; then
    echo "ERROR: SBSP_ROUTER_ID not set"
    exit 1
fi

# Wait for ContainerLab to wire up the veth interfaces
sleep 3

LAST_OCTET=$(echo "$ROUTER_ID" | cut -d. -f4)

# Skip eth0 — that is the ContainerLab management interface (172.30.x.x).
# Data-plane interfaces start at eth1.
IFACE_ARGS=""
IFACE_NUM=1

for iface in $(ls /sys/class/net/ | grep -E '^eth[1-9][0-9]*$' | sort); do
    # Assign IP: 10.0.<link_num>.<last_octet>/24
    IP="10.0.${IFACE_NUM}.${LAST_OCTET}"
    ip addr flush dev "$iface" 2>/dev/null || true
    ip addr add "${IP}/24" dev "$iface" 2>/dev/null || true
    ip link set "$iface" up 2>/dev/null || true
    echo "  Configured ${iface} -> ${IP}/24"

    if [ -n "$IFACE_ARGS" ]; then
        IFACE_ARGS="${IFACE_ARGS},${iface}:${IP}:10"
    else
        IFACE_ARGS="${iface}:${IP}:10"
    fi

    IFACE_NUM=$((IFACE_NUM + 1))
done

if [ -z "$IFACE_ARGS" ]; then
    echo "WARNING: No data-plane interfaces found (eth1+). Waiting and retrying..."
    sleep 5
    for iface in $(ls /sys/class/net/ | grep -E '^eth[0-9]+' | sort); do
        IP="10.0.${IFACE_NUM}.${LAST_OCTET}"
        ip addr add "${IP}/24" dev "$iface" 2>/dev/null || true
        ip link set "$iface" up 2>/dev/null || true
        IFACE_ARGS="${IFACE_ARGS:+${IFACE_ARGS},}${iface}:${IP}:10"
        IFACE_NUM=$((IFACE_NUM + 1))
    done
fi

echo "SBSP starting: router-id=$ROUTER_ID area=$AREA interfaces=$IFACE_ARGS"

exec python -m sbsp.daemon.main \
    --router-id   "$ROUTER_ID" \
    --area        "$AREA" \
    --interfaces  "$IFACE_ARGS" \
    --log-level   "$LOG_LEVEL"
