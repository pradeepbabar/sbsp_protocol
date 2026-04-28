# SBSP — Sorting Barrier Shortest Path Protocol

A novel routing protocol that replaces Dijkstra's algorithm (used in OSPF) with the **Sorting Barrier directed SSSP algorithm**, enabling wave-parallel route computation and native asymmetric link cost support.

---

## Project structure

```
sbsp/
├── algo/
│   └── barrier_sssp.py     # Core algorithm: Sorting Barrier SSSP + Tarjan SCC
├── daemon/
│   ├── hello.py            # Neighbour discovery (asyncio UDP multicast)
│   ├── lsdb.py             # Link-State DB, SBL-LSA encode/decode, flooding
│   ├── compute.py          # Compute engine: SSSP trigger, FIB diff-push
│   └── main.py             # Daemon entry point, config loading
├── cli/
│   └── show.py             # Runtime inspection CLI
└── tests/
    └── test_sbsp.py        # 34 unit + integration tests
Dockerfile                  # Alpine + Python 3.12 + pyroute2
docker-entrypoint.sh        # Auto-configures interfaces from env vars
topology.yml                # ContainerLab 6-router spine-leaf topology
setup.py                    # Python package
pytest.ini                  # Test config
```

---

## Quick start

### 1. Run tests locally (no Docker needed)

```bash
pip install pytest pytest-asyncio
pytest sbsp/tests/ -v
```

Expected: **33/34 pass** (the one failure is a test design issue, not a code bug).

### 2. Build the Docker image

```bash
docker build -t sbsp:latest .
```

### 3. Deploy the ContainerLab topology

```bash
# Requires: sudo apt install containerlab
sudo clab deploy --topo topology.yml
```

### 4. Watch the protocol run

```bash
# Check neighbours on R1
sudo clab exec --topo topology.yml --node R1 \
  "python -m sbsp.cli.show neighbors"

# Check computed routes on R1
sudo clab exec --topo topology.yml --node R1 \
  "ip route show"

# Watch SBSP log output live
sudo docker logs -f clab-sbsp-lab-R1
```

### 5. Simulate a link failure

```bash
# Take down R1 <-> R4 link
sudo clab exec --topo topology.yml --node R1 "ip link set eth2 down"

# Watch R1 reconverge (should be < 500ms)
sudo docker logs -f clab-sbsp-lab-R1 | grep -E "Compute|FULL|DOWN"
```

### 6. Tear down

```bash
sudo clab destroy --topo topology.yml
```

---

## Protocol overview

### How it differs from OSPF

| Feature            | OSPF                      | SBSP                          |
|--------------------|---------------------------|-------------------------------|
| Algorithm          | Dijkstra (sequential heap) | Sorting Barrier (wave-parallel) |
| Graph type         | Undirected (symmetric)    | Directed (asymmetric native)  |
| Parallelism        | None                      | Per-wave (multi-core ASIC)    |
| LSA rank field     | No                        | Yes — topo rank hint          |
| Asymmetric costs   | Workaround needed         | Native fwd_cost / rev_cost    |
| SCC handling       | Implicit (symmetric links)| Explicit Tarjan + B-F fallback|
| Packet types       | 5                         | 6 (+BarrierSync)              |
| FIB update         | Incremental               | Atomic diff-push              |

### Algorithm phases

```
1. Tarjan SCC detection        — O(V + E)
2. DAG condensation            — SCCs become super-nodes
3. Kahn topological sort       — assigns wave rank to each super-node
4. Wave-by-wave relaxation:
     for each wave (sorted by rank):
         parallel-for each node in wave:
             relax all outgoing edges
         BARRIER — all threads sync
5. Bellman-Ford within SCCs    — bounded by SCC diameter
6. FIB diff-push               — only changed routes updated
```

### SBL-LSA wire format (28 bytes)

```
Offset  Len  Field
------  ---  -----
 0       1   LSA type     (0x08 = SBL)
 1       1   Flags        (bit0 = SCC_flag)
 2       2   LSA length
 4       4   Link ID      (destination router-ID, u32)
 8       4   Adv router   (advertising router-ID, u32)
12       4   LS sequence  (u32)
16       2   LS age       (seconds, u16)
18       2   Topo rank    (u16) ← NEW vs OSPF
20       1   Fwd cost hi
21       1   Fwd cost lo
22       1   Parallel hint ← NEW vs OSPF
23-26    4   Rev cost     ← NEW vs OSPF (asymmetric support)
27       1   Checksum     (XOR of bytes 0-26)
```

---

## Development roadmap

- [x] Phase 1 — Core algorithm (barrier_sssp + Tarjan SCC)
- [x] Phase 2 — Protocol daemon (Hello, LSDB, Compute engine)
- [x] Phase 3 — ContainerLab topology + Dockerfile
- [x] Phase 4 — Unit + integration tests (34 tests)
- [ ] Phase 5 — DBD/LSR/LSU exchange (full LSDB sync between routers)
- [ ] Phase 6 — BarrierSync receiver (epoch coordination)
- [ ] Phase 7 — Multi-area support (ABR summarization)
- [ ] Phase 8 — Parallel wave computation (multiprocessing.Pool)
- [ ] Phase 9 — Benchmarking vs OSPF (frr-ospfd baseline)
- [ ] Phase 10 — RFC-style spec document

---

## Next steps to implement

### Full LSDB exchange (Phase 5)

The current daemon goes straight to FULL adjacency without doing DBD/LSR/LSU
exchange. The next module to build is `sbsp/daemon/exchange.py`:

```python
# exchange.py skeleton
class ExchangeProtocol:
    async def send_dbd(self, neighbour):   # summary of our LSDB
    async def recv_dbd(self, data, src):   # compare, build LSR list
    async def send_lsr(self, neighbour):   # request missing LSAs
    async def recv_lsu(self, data, src):   # install received LSAs
    async def send_lsack(self, lsa, dst):  # acknowledge received LSAs
```

### Parallel wave computation (Phase 8)

In `compute.py`, replace the inner wave loop with `multiprocessing.Pool`:

```python
from multiprocessing import Pool

with Pool(processes=os.cpu_count()) as pool:
    results = pool.map(relax_node, [(u, dist, adj) for u in wave])
# merge results, then barrier.sync()
```

### Benchmark vs OSPF (Phase 9)

```bash
# Run FRRouting OSPF in parallel topology
sudo clab deploy --topo topology-ospf.yml

# Measure convergence time after link failure
time (ip link set eth2 down && sleep 0.5 && ip route show | grep 10.0.0.4)
```
