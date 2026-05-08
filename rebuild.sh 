#!/bin/bash
# rebuild_and_deploy.sh
# Place this in your project directory alongside Dockerfile and topology.yml
# Run: chmod +x rebuild_and_deploy.sh && ./rebuild_and_deploy.sh

set -e
cd "$(dirname "$0")"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  OK${NC}: $1"; }
fail() { echo -e "${RED}  FAIL${NC}: $1"; exit 1; }
info() { echo -e "${YELLOW}>>>${NC} $1"; }

echo "========================================"
echo "  SBSP Rebuild + Deploy"
echo "========================================"

# ---- Check files exist ----
info "Checking files..."
[ -f Dockerfile ]             || fail "Dockerfile missing"
[ -f docker-entrypoint.sh ]   || fail "docker-entrypoint.sh missing"
[ -f topology.yml ]           || fail "topology.yml missing"
[ -f pyproject.toml ]         || fail "pyproject.toml missing"
[ -d sbsp/daemon ]            || fail "sbsp/daemon/ missing"
ok "All files present"

# ---- Check Dockerfile is new version ----
info "Verifying Dockerfile..."
grep -q "PYTHONPATH=/app" Dockerfile       || fail "Dockerfile is OLD - missing PYTHONPATH=/ app"
grep -q "pyproject\|setup.py\|pip install -e" Dockerfile && fail "Dockerfile has old pip install -e" || true
ok "Dockerfile is correct"

# ---- Check entrypoint is new version ----
info "Verifying docker-entrypoint.sh..."
grep -q "grep -c\|MAX_WAIT\|WAITED" docker-entrypoint.sh && fail "entrypoint is OLD version" || true
grep -q "PYTHONPATH=/app" docker-entrypoint.sh || fail "entrypoint missing PYTHONPATH"
grep -q 'ip -o link show' docker-entrypoint.sh || fail "entrypoint missing ip -o link show"
ok "Entrypoint is correct"

# ---- Destroy old lab completely ----
info "Destroying old lab..."
sudo clab destroy --topo topology.yml --cleanup 2>/dev/null || true
docker ps -a --filter "name=clab-sbsp" --format "{{.Names}}" | xargs -r docker rm -f 2>/dev/null || true
docker network rm sbsp-mgmt 2>/dev/null || true
sudo rm -rf clab-sbsp-lab/ 2>/dev/null || true
ok "Old lab destroyed"

# ---- Remove old images ----
info "Removing old sbsp images..."
docker images sbsp --format "{{.ID}}" | xargs -r docker rmi -f 2>/dev/null || true
ok "Old images removed"

# ---- Build fresh ----
info "Building fresh image (60-90s)..."
docker build --no-cache -t sbsp:latest . 2>&1 | tee /tmp/sbsp_build.log | grep -E "Step|RUN|COPY|OK:|ERROR|error" || true

grep -q "OK: sbsp.daemon.main importable" /tmp/sbsp_build.log \
    || fail "Build-time import check failed! Run: cat /tmp/sbsp_build.log"
ok "Build-time import verified"

# ---- Smoke test ----
info "Smoke testing container..."
OUT=$(docker run --rm sbsp:latest python -c \
    "from sbsp.daemon.main import main; print('SMOKE_OK')" 2>&1)
echo "$OUT" | grep -q "SMOKE_OK" || fail "Smoke test failed: $OUT"
ok "Container smoke test passed"

# ---- Deploy ----
info "Deploying lab..."
sudo clab deploy --topo topology.yml

echo ""
echo "========================================"
echo -e "${GREEN}  Lab deployed successfully!${NC}"
echo "========================================"
echo ""
echo "Useful commands:"
echo "  sudo clab inspect --topo topology.yml"
echo "  sudo docker logs clab-sbsp-lab-R1"
echo "  sudo docker exec -it clab-sbsp-lab-R1 ip route"
echo "  sudo clab destroy --topo topology.yml --cleanup"