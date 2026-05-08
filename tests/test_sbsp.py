"""
tests/test_sbsp.py
Full test suite for SBSP algorithm and protocol components.

Run: pytest tests/ -v
"""

import asyncio
import math
import pytest

from sbsp.algo.barrier_sssp import (
    sorting_barrier_sssp,
    TarjanSCC,
    Graph,
    next_hop,
    reconstruct_path,
)
from sbsp.daemon.lsdb import LSDB, SblLsa, _ip_to_int, _int_to_ip
from sbsp.daemon.hello import encode_hello, decode_hello
from sbsp.daemon.compute import ComputeEngine, KernelFIB, Route


# ===========================================================================
# Algorithm tests
# ===========================================================================

class TestBarrierSSSP:

    def test_simple_linear(self):
        """R1 -> R2 -> R3, straight line."""
        edges = [("R1","R2",5), ("R2","R3",3)]
        dist, prev = sorting_barrier_sssp(edges, "R1")
        assert dist["R2"] == 5
        assert dist["R3"] == 8

    def test_parallel_paths(self):
        """R1 has two paths to R4; shorter one should win."""
        edges = [
            ("R1","R2",5), ("R1","R3",3),
            ("R3","R2",1), ("R2","R4",2), ("R3","R4",6)
        ]
        dist, prev = sorting_barrier_sssp(edges, "R1")
        assert dist["R2"] == 4   # R1->R3->R2
        assert dist["R4"] == 6   # R1->R3->R2->R4

    def test_asymmetric_costs(self):
        """Directed costs A->B and B->A are different."""
        edges = [("A","B",1), ("B","A",10)]
        dist_a, _ = sorting_barrier_sssp(edges, "A")
        dist_b, _ = sorting_barrier_sssp(edges, "B")
        assert dist_a["B"] == 1
        assert dist_b["A"] == 10

    def test_unreachable_node(self):
        """Node with no incoming edges from source is unreachable."""
        edges = [("R1","R2",1), ("R3","R4",1)]  # R3/R4 disconnected
        dist, _ = sorting_barrier_sssp(edges, "R1")
        assert dist["R2"] == 1
        assert dist["R3"] == math.inf
        assert dist["R4"] == math.inf

    def test_single_node(self):
        """Source only — distance to itself is 0."""
        dist, _ = sorting_barrier_sssp([], "R1")
        assert dist["R1"] == 0.0

    def test_six_node_spine_leaf(self):
        """Spine: R1,R2. Leaves: R3,R4,R5,R6."""
        edges = [
            ("R1","R3",1), ("R1","R4",1), ("R1","R5",1),
            ("R2","R4",1), ("R2","R5",1), ("R2","R6",1),
            ("R3","R6",2),
        ]
        dist, prev = sorting_barrier_sssp(edges, "R1")
        assert dist["R3"] == 1
        assert dist["R4"] == 1
        assert dist["R5"] == 1
        # R1->R3->R6 = 1+2 = 3
        assert dist["R6"] == 3

    def test_weighted_diamond(self):
        """Classic diamond: A->B (1), A->C (4), B->D (1), C->D (1)."""
        edges = [("A","B",1), ("A","C",4), ("B","D",1), ("C","D",1)]
        dist, prev = sorting_barrier_sssp(edges, "A")
        assert dist["D"] == 2    # A->B->D
        nh = next_hop(prev, "A", "D")
        assert nh == "B"

    def test_cycle_with_bf_fallback(self):
        """Network with a cycle: R1->R2->R3->R1. Source R1 should reach all."""
        edges = [("R1","R2",5), ("R2","R3",5), ("R3","R1",5),
                 ("R1","R4",1)]
        dist, _ = sorting_barrier_sssp(edges, "R1")
        assert dist["R4"] == 1
        assert dist["R2"] == 5
        assert dist["R3"] == 10

    def test_zero_weight_edge(self):
        edges = [("A","B",0), ("B","C",5)]
        dist, _ = sorting_barrier_sssp(edges, "A")
        assert dist["B"] == 0
        assert dist["C"] == 5

    def test_large_linear_chain(self):
        """50-node chain — verifies no stack overflow."""
        n = 50
        nodes = [f"R{i}" for i in range(n)]
        edges = [(nodes[i], nodes[i+1], 1) for i in range(n-1)]
        dist, _ = sorting_barrier_sssp(edges, nodes[0])
        assert dist[nodes[-1]] == n - 1


class TestTarjanSCC:

    def test_no_cycles(self):
        """DAG: each SCC should be size 1."""
        g = Graph.from_edge_list([("A","B",1),("B","C",1)])
        sccs = TarjanSCC(g).run()
        # All SCCs should be singletons
        assert all(len(s) == 1 for s in sccs)

    def test_simple_cycle(self):
        """A->B->A is one SCC of size 2."""
        g = Graph.from_edge_list([("A","B",1),("B","A",1)])
        sccs = TarjanSCC(g).run()
        large = [s for s in sccs if len(s) == 2]
        assert len(large) == 1
        assert large[0] == frozenset({"A","B"})

    def test_two_separate_sccs(self):
        """(A<->B) and (C<->D) are two independent SCCs."""
        g = Graph.from_edge_list([
            ("A","B",1),("B","A",1),
            ("C","D",1),("D","C",1),
            ("B","C",1),
        ])
        sccs = TarjanSCC(g).run()
        large = sorted([s for s in sccs if len(s) == 2], key=lambda s: min(s))
        assert len(large) == 2


class TestPathReconstruction:

    def test_reconstruct(self):
        edges = [("A","B",1),("B","C",2),("C","D",3)]
        dist, prev = sorting_barrier_sssp(edges, "A")
        path = reconstruct_path(prev, "D")
        assert path == ["A","B","C","D"]

    def test_next_hop(self):
        edges = [("A","B",1),("B","C",2)]
        _, prev = sorting_barrier_sssp(edges, "A")
        assert next_hop(prev, "A", "C") == "B"
        assert next_hop(prev, "A", "B") == "B"


# ===========================================================================
# LSDB tests
# ===========================================================================

class TestSblLsa:

    def test_encode_decode_roundtrip(self):
        lsa = SblLsa(
            link_id       = "10.0.0.2",
            adv_router    = "10.0.0.1",
            sequence      = 42,
            age           = 0.0,
            topo_rank     = 3,
            fwd_cost      = 10,
            rev_cost      = 10,
            parallel_hint = 1,
            scc_flag      = False,
        )
        encoded = lsa._pack_body()
        assert len(encoded) == 23

    def test_ip_int_roundtrip(self):
        for ip in ["10.0.0.1", "192.168.1.100", "172.16.0.50", "0.0.0.0"]:
            assert _int_to_ip(_ip_to_int(ip)) == ip

    def test_scc_flag_encoding(self):
        lsa = SblLsa(
            link_id="10.0.0.3", adv_router="10.0.0.1",
            sequence=1, age=0, topo_rank=0,
            fwd_cost=100, rev_cost=100, parallel_hint=0,
            scc_flag=True,
        )
        body = lsa._pack_body()
        # Byte index 1 is flags field
        assert body[1] & 0x01 == 1   # SCC flag set


@pytest.mark.asyncio
class TestLSDB:

    async def test_install_and_retrieve(self):
        lsdb = LSDB(router_id="10.0.0.1")
        lsa = SblLsa(
            link_id="10.0.0.2", adv_router="10.0.0.1",
            sequence=1, age=0, topo_rank=0,
            fwd_cost=10, rev_cost=10, parallel_hint=0,
        )
        changed = await lsdb.install_lsa(lsa)
        assert changed
        edges = lsdb.get_edge_list()
        assert ("10.0.0.1", "10.0.0.2", 10.0) in edges

    async def test_older_sequence_rejected(self):
        lsdb = LSDB(router_id="10.0.0.1")
        lsa1 = SblLsa("10.0.0.2","10.0.0.1",5, 0,0,10,10,0)
        lsa2 = SblLsa("10.0.0.2","10.0.0.1",3, 0,0,99,99,0)
        await lsdb.install_lsa(lsa1)
        changed = await lsdb.install_lsa(lsa2)
        assert not changed
        # Cost should still be 10, not 99
        edges = lsdb.get_edge_list()
        assert ("10.0.0.1","10.0.0.2",10.0) in edges

    async def test_change_callback(self):
        lsdb = LSDB(router_id="10.0.0.1")
        called = []
        lsdb.register_change_callback(lambda: called.append(1))
        lsa = SblLsa("10.0.0.2","10.0.0.1",1, 0,0,10,10,0)
        await lsdb.install_lsa(lsa)
        assert len(called) == 1

    async def test_originate_lsa(self):
        lsdb = LSDB(router_id="10.0.0.1")
        lsa = await lsdb.originate_lsa("10.0.0.2", fwd_cost=5)
        assert lsa.adv_router == "10.0.0.1"
        assert lsa.fwd_cost == 5
        assert lsa.sequence == 1

    async def test_multiple_lsas_build_graph(self):
        lsdb = LSDB(router_id="10.0.0.1")
        for link, cost in [("10.0.0.2",5),("10.0.0.3",10),("10.0.0.4",1)]:
            await lsdb.originate_lsa(link, fwd_cost=cost)
        edges = lsdb.get_edge_list()
        assert len(edges) == 3


# ===========================================================================
# Hello protocol tests
# ===========================================================================

class TestHelloPacket:

    def test_encode_decode_roundtrip(self):
        pkt = encode_hello(
            router_id  = "10.0.0.1",
            area_id    = "0.0.0.0",
            iface_ip   = "10.0.0.1",
            iface_cost = 10,
            topo_rank  = 2,
        )
        assert len(pkt) == 32
        decoded = decode_hello(pkt)
        assert decoded is not None
        assert decoded["router_id"] == "10.0.0.1"
        assert decoded["cost"] == 10
        assert decoded["topo_rank"] == 2

    def test_bad_checksum_rejected(self):
        pkt = bytearray(encode_hello("10.0.0.1","0.0.0.0","10.0.0.1",10))
        pkt[28] ^= 0xFF   # corrupt checksum
        assert decode_hello(bytes(pkt)) is None

    def test_too_short_rejected(self):
        assert decode_hello(b"\x01\x01\x00\x10") is None

    def test_dr_eligible_flag(self):
        pkt = encode_hello("10.0.0.1","0.0.0.0","10.0.0.1",10,dr_eligible=True)
        decoded = decode_hello(pkt)
        assert decoded["dr_eligible"] is True

        pkt2 = encode_hello("10.0.0.1","0.0.0.0","10.0.0.1",10,dr_eligible=False)
        decoded2 = decode_hello(pkt2)
        assert decoded2["dr_eligible"] is False


# ===========================================================================
# Compute engine tests (dry-run, no kernel writes)
# ===========================================================================

class StubFIB(KernelFIB):
    """KernelFIB that records calls instead of writing to kernel."""
    def __init__(self):
        self._ipr = None
        self.added: list   = []
        self.deleted: list = []

    def add_route(self, route: Route):
        self.added.append(route)

    def delete_route(self, route: Route):
        self.deleted.append(route)


@pytest.mark.asyncio
class TestComputeEngine:

    async def _make_engine(self):
        lsdb = LSDB(router_id="10.0.0.1")
        fib  = StubFIB()
        engine = ComputeEngine(
            lsdb       = lsdb,
            router_id  = "10.0.0.1",
            interfaces = [{"name":"eth0","ip":"10.0.0.1","cost":10}],
            fib        = fib,
        )
        lsdb.register_change_callback(engine.schedule_compute)
        return lsdb, fib, engine

    async def test_routes_installed_after_compute(self):
        lsdb, fib, engine = await self._make_engine()

        # Build a 3-node topology
        await lsdb.originate_lsa("10.0.0.2", fwd_cost=5)
        await lsdb.originate_lsa("10.0.0.3", fwd_cost=10)

        # LSA from R2 to R3
        lsa = SblLsa("10.0.0.3","10.0.0.2",1, 0,0,3,3,0)
        await lsdb.install_lsa(lsa)

        # Manually trigger compute
        await engine._run_compute()

        assert len(fib.added) >= 2
        prefixes = [r.prefix for r in fib.added]
        assert "10.0.0.2" in prefixes
        assert "10.0.0.3" in prefixes

    async def test_route_removed_when_lsa_withdrawn(self):
        lsdb, fib, engine = await self._make_engine()
        await lsdb.originate_lsa("10.0.0.2", fwd_cost=5)
        await engine._run_compute()
        initial_adds = len(fib.added)
        assert initial_adds > 0

        # Withdraw: force-expire the LSA so get_edge_list excludes it
        lsa = lsdb.get_lsa("10.0.0.2", "10.0.0.1")
        if lsa:
            lsa.age = 3600  # mark as max-age expired
        fib.added.clear()
        await engine._run_compute()

        # After expiry, the route to 10.0.0.2 should be deleted
        assert len(fib.deleted) > 0

    async def test_asymmetric_route_selection(self):
        """Source 10.0.0.1 has two paths to 10.0.0.4; lower cost wins."""
        lsdb, fib, engine = await self._make_engine()

        # 10.0.0.1 -> 10.0.0.2 (cost 1)
        await lsdb.originate_lsa("10.0.0.2", fwd_cost=1)
        # 10.0.0.1 -> 10.0.0.3 (cost 1)
        await lsdb.originate_lsa("10.0.0.3", fwd_cost=1)
        # 10.0.0.2 -> 10.0.0.4 (cost 1) via R2
        await lsdb.install_lsa(SblLsa("10.0.0.4","10.0.0.2",1,0,0,1,1,0))
        # 10.0.0.3 -> 10.0.0.4 (cost 10) via R3 — more expensive
        await lsdb.install_lsa(SblLsa("10.0.0.4","10.0.0.3",1,0,0,10,10,0))

        await engine._run_compute()

        r4_routes = [r for r in fib.added if r.prefix == "10.0.0.4"]
        assert r4_routes, "No route to 10.0.0.4"
        best = min(r4_routes, key=lambda r: r.metric)
        assert best.metric == 2.0     # via R2: 1+1=2

    async def test_show_routes(self):
        lsdb, fib, engine = await self._make_engine()
        await lsdb.originate_lsa("10.0.0.2", fwd_cost=5)
        await engine._run_compute()
        output = engine.show_routes()
        assert "10.0.0.2" in output

    async def test_stats_updated(self):
        lsdb, fib, engine = await self._make_engine()
        await lsdb.originate_lsa("10.0.0.2", fwd_cost=5)
        await engine._run_compute()
        assert engine.stats["compute_runs"] == 1
        assert engine.stats["last_compute_ms"] >= 0


# ===========================================================================
# Integration: end-to-end algorithm -> LSDB -> compute
# ===========================================================================

@pytest.mark.asyncio
class TestEndToEnd:

    async def test_full_pipeline_6_node(self):
        """
        Simulate a 6-router spine-leaf topology entirely in-process.
        R1 is our router. Verify routes to all 5 other routers.
        """
        lsdb = LSDB(router_id="10.0.0.1")
        fib  = StubFIB()
        engine = ComputeEngine(
            lsdb       = lsdb,
            router_id  = "10.0.0.1",
            interfaces = [{"name":"eth0","ip":"10.0.0.1","cost":1}],
            fib        = fib,
        )

        # Spine-leaf links (all directed from originating router)
        topo = [
            # R1 links
            ("10.0.0.1","10.0.0.3",1), ("10.0.0.1","10.0.0.4",1),
            ("10.0.0.1","10.0.0.5",1),
            # R2 links
            ("10.0.0.2","10.0.0.4",1), ("10.0.0.2","10.0.0.5",1),
            ("10.0.0.2","10.0.0.6",1),
            # East-west
            ("10.0.0.3","10.0.0.6",2),
        ]
        for src, dst, cost in topo:
            await lsdb.install_lsa(
                SblLsa(dst, src, 1, 0, 0, cost, cost, 0)
            )

        await engine._run_compute()

        installed = {r.prefix for r in fib.added}
        # R1 can reach R3,R4,R5 directly; R6 via R3; R2 not reachable (no path)
        assert "10.0.0.3" in installed
        assert "10.0.0.4" in installed
        assert "10.0.0.5" in installed
        assert "10.0.0.6" in installed

    async def test_link_failure_reconvergence(self):
        """Remove a link and verify the route changes."""
        lsdb = LSDB(router_id="10.0.0.1")
        fib  = StubFIB()
        engine = ComputeEngine(
            lsdb       = lsdb,
            router_id  = "10.0.0.1",
            interfaces = [{"name":"eth0","ip":"10.0.0.1","cost":1}],
            fib        = fib,
        )

        # Primary: R1->R2->R4 (cost 2)
        # Backup:  R1->R3->R4 (cost 10)
        await lsdb.install_lsa(SblLsa("10.0.0.2","10.0.0.1",1,0,0,1,1,0))
        await lsdb.install_lsa(SblLsa("10.0.0.4","10.0.0.2",1,0,0,1,1,0))
        await lsdb.install_lsa(SblLsa("10.0.0.3","10.0.0.1",1,0,0,5,5,0))
        await lsdb.install_lsa(SblLsa("10.0.0.4","10.0.0.3",1,0,0,5,5,0))

        await engine._run_compute()
        r4_primary = [r for r in fib.added if r.prefix == "10.0.0.4"]
        assert r4_primary
        assert r4_primary[0].metric == 2.0   # via R2

        # Simulate R2 going down — withdraw R1->R2 and R2->R4
        await lsdb.withdraw_lsa("10.0.0.2")
        for lsa in lsdb.get_all_lsas():
            if lsa.adv_router == "10.0.0.2":
                lsa.age = 3600   # force expire

        fib.added.clear()
        await engine._run_compute()

        r4_backup = [r for r in fib.added if r.prefix == "10.0.0.4"]
        # After R2 failure, R4 should be reachable via R3 (cost 10)
        assert r4_backup
        assert r4_backup[0].metric == 10.0