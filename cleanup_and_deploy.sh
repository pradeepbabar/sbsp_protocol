#!/bin/bash
# cleanup_and_deploy.sh
# Full nuclear cleanup of all stale ContainerLab state, then fresh deploy.
# Run from the directory containing topology.yml

set -e
TOPO="topology.yml"
LAB_NAME="sbsp-lab"
LAB_DIR="clab-${LAB_NAME}"

echo "=== Step 1: Kill any running SBSP containers ==="
docker ps -a --filter "name=clab-sbsp" --format "{{.Names}}" | \
  xargs -r docker rm -f 2>/dev/null && echo "Containers removed" || echo "No containers to remove"

echo ""
echo "=== Step 2: Remove stale ContainerLab lab directory ==="
if [ -d "$LAB_DIR" ]; then
    sudo rm -rf "$LAB_DIR"
    echo "Removed $LAB_DIR"
else
    echo "No stale lab directory found"
fi

echo ""
echo "=== Step 3: Remove stale Docker network ==="
docker network ls --filter "name=sbsp-mgmt" --format "{{.Name}}" | \
  xargs -r docker network rm 2>/dev/null && echo "Network removed" || echo "No network to remove"

echo ""
echo "=== Step 4: Clean up /etc/hosts entries ==="
sudo sed -i '/clab-sbsp/d' /etc/hosts 2>/dev/null && echo "Hosts cleaned" || echo "Nothing to clean"

echo ""
echo "=== Step 5: Verify topology has no issues ==="
echo "Links defined:"
grep 'endpoints:' "$TOPO" | grep -v '#'
echo ""
echo "Interface uniqueness check:"
DUPS=$(grep 'endpoints:' "$TOPO" | grep -v '#' | \
  grep -oE 'R[0-9]:eth[0-9]+' | sort | uniq -d)
if [ -n "$DUPS" ]; then
    echo "ERROR: Duplicate interfaces found: $DUPS"
    exit 1
else
    echo "OK - no duplicate interfaces"
fi

echo ""
echo "=== Step 6: Deploy fresh ==="
sudo clab deploy --topo "$TOPO"

echo ""
echo "=== Done ==="