"""
sbsp/daemon/compute.py
SBSP Compute Engine.

Responsibilities:
  1. Listen for topology-change signals from LSDB
  2. Apply SPF delay timer (100 ms hold-down to batch rapid changes)
  3. Run sorting_barrier_sssp() on the current LSDB edge list
  4. Diff new routes against current FIB
  5. Atomically push adds/deletes to kernel via pyroute2 (Netlink)
  6. Emit BarrierSync packet to signal computation epoch to peers

BarrierSync packet (16 bytes, big-endian):
  Offset  Len  Field
  ------  ---  -----
   0       1   Packet type  (0x06 = BarrierSync)
   1       1   Version      (0x01)
   2       2   Length       (u16)
   4       4   Router ID    (u32)
   8       4   Epoch        (u32, monotonically increasing)
  12       4   Checksum     (u32)
  Total: 16 bytes
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..algo.barrier_sssp import sorting_barrier_sssp, next_hop
from .lsdb import LSDB

log = logging.getLogger(__name__)

SPF_DELAY       = 0.1        # seconds — hold-down before recompute
PKT_BARRIER     = 0x06
BARRIER_VERSION = 0x01
BARRIER_SIZE    = 16
BARRIER_PORT    = 9992
MCAST_GROUP     = "224.0.0.91"


# ---------------------------------------------------------------------------
# Route data class
# ---------------------------------------------------------------------------

@dataclass
class Route:
    prefix:   str          # destination, e.g. "10.0.0.3"
    next_hop: str          # e.g. "10.0.1.1"
    metric:   float
    via_iface: str = ""    # outgoing interface name

    def key(self) -> str:
        return f"{self.prefix}/32"

    def __eq__(self, other):
        return (self.prefix == other.prefix and
                self.next_hop == other.next_hop and
                self.metric == other.metric)


# ---------------------------------------------------------------------------
# FIB interface (kernel via pyroute2, or stub for testing)
# ---------------------------------------------------------------------------

class KernelFIB:
    """
    Wraps pyroute2.IPRoute for Netlink FIB operations.
    Falls back to a no-op stub if pyroute2 is not installed.
    """

    def __init__(self):
        self._ipr = None
        try:
            from pyroute2 import IPRoute
            self._ipr = IPRoute()
            log.info("pyroute2 IPRoute available — Netlink FIB writes enabled")
        except ImportError:
            log.warning("pyroute2 not installed — FIB writes disabled (dry-run mode)")

    def add_route(self, route: Route):
        log.info("FIB ADD  %s via %s metric %.0f", route.key(), route.next_hop, route.metric)
        if self._ipr is None:
            return
        try:
            self._ipr.route(
                "add",
                dst     = route.key(),
                gateway = route.next_hop,
                metrics = {"metric": int(route.metric)},
            )
        except Exception as e:
            # Route may already exist; try replace
            try:
                self._ipr.route(
                    "replace",
                    dst     = route.key(),
                    gateway = route.next_hop,
                    metrics = {"metric": int(route.metric)},
                )
            except Exception as e2:
                log.error("FIB add/replace failed for %s: %s", route.key(), e2)

    def delete_route(self, route: Route):
        log.info("FIB DEL  %s via %s", route.key(), route.next_hop)
        if self._ipr is None:
            return
        try:
            self._ipr.route("del", dst=route.key(), gateway=route.next_hop)
        except Exception as e:
            log.debug("FIB delete skipped for %s: %s", route.key(), e)

    def close(self):
        if self._ipr:
            self._ipr.close()


# ---------------------------------------------------------------------------
# Compute Engine
# ---------------------------------------------------------------------------

class ComputeEngine:
    """
    Triggered by LSDB changes.
    Runs Sorting Barrier SSSP and pushes diffs to FIB.
    """

    def __init__(
        self,
        lsdb:      LSDB,
        router_id: str,
        interfaces: List[Dict],           # same list as HelloProtocol
        fib:       Optional[KernelFIB] = None,
    ):
        self.lsdb       = lsdb
        self.router_id  = router_id
        self.interfaces = interfaces
        self.fib        = fib or KernelFIB()

        self._pending      = False
        self._epoch        = 0
        self._last_compute = 0.0
        self._current_fib: Dict[str, Route] = {}   # prefix -> Route

        # Performance stats
        self.stats = {
            "compute_runs":   0,
            "routes_added":   0,
            "routes_removed": 0,
            "last_compute_ms": 0.0,
        }

    def schedule_compute(self):
        """Called by LSDB change callback (sync). Schedules a delayed SPF run."""
        if not self._pending:
            self._pending = True
            asyncio.get_event_loop().call_later(SPF_DELAY, self._trigger)

    def _trigger(self):
        asyncio.get_event_loop().create_task(self._run_compute())

    async def run(self):
        """Background runner — just keeps the engine alive."""
        log.info("Compute engine started for %s", self.router_id)
        while True:
            await asyncio.sleep(3600)   # periodic full recompute (safety net)
            self.schedule_compute()

    # ---- Core computation ---------------------------------------------------

    async def _run_compute(self):
        self._pending = False
        t0 = time.monotonic()

        edges = self.lsdb.get_edge_list()
        if not edges:
            log.debug("No edges in LSDB — skipping compute")
            return

        log.info("Running SBSP compute: %d edges, source=%s", len(edges), self.router_id)

        # ---- Sorting Barrier SSSP -------------------------------------------
        try:
            dist, prev = sorting_barrier_sssp(edges, self.router_id)
        except Exception as e:
            log.error("SSSP failed: %s", e)
            return

        # ---- Build new route table ------------------------------------------
        new_routes: Dict[str, Route] = {}
        for node, distance in dist.items():
            if node == self.router_id:
                continue
            if distance == float("inf"):
                continue

            nh = next_hop(prev, self.router_id, node)
            if nh is None:
                continue

            iface = self._iface_for_nexthop(nh)
            new_routes[node] = Route(
                prefix    = node,
                next_hop  = nh,
                metric    = distance,
                via_iface = iface,
            )

        # ---- Diff against current FIB ---------------------------------------
        to_add    = []
        to_delete = []

        for prefix, route in new_routes.items():
            existing = self._current_fib.get(prefix)
            if existing is None or existing != route:
                to_add.append(route)
                if existing:
                    to_delete.append(existing)   # replace = del + add

        for prefix, route in self._current_fib.items():
            if prefix not in new_routes:
                to_delete.append(route)

        # ---- Atomic push to FIB (delete first, then add) --------------------
        for route in to_delete:
            self.fib.delete_route(route)

        for route in to_add:
            self.fib.add_route(route)

        self._current_fib = new_routes

        # ---- Stats ----------------------------------------------------------
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.stats["compute_runs"]   += 1
        self.stats["routes_added"]   += len(to_add)
        self.stats["routes_removed"] += len(to_delete)
        self.stats["last_compute_ms"] = elapsed_ms
        self._epoch += 1
        self._last_compute = time.monotonic()

        log.info(
            "Compute done in %.2f ms | epoch=%d | routes=%d (+%d -%d)",
            elapsed_ms, self._epoch,
            len(new_routes), len(to_add), len(to_delete),
        )

        # ---- Broadcast BarrierSync to peers ---------------------------------
        await self._send_barrier_sync()

    # ---- BarrierSync --------------------------------------------------------

    async def _send_barrier_sync(self):
        """
        Broadcast BarrierSync so all routers know which epoch to compute from.
        In a full implementation this would be sent on each adjacency unicast.
        Here we use multicast for simplicity.
        """
        pkt = self._encode_barrier_sync()
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setblocking(False)
        try:
            for iface in self.interfaces:
                try:
                    sock.setsockopt(
                        socket.IPPROTO_IP,
                        socket.IP_MULTICAST_IF,
                        socket.inet_aton(iface["ip"]),
                    )
                    await loop.sock_sendto(sock, pkt, (MCAST_GROUP, BARRIER_PORT))
                except OSError:
                    pass
        finally:
            sock.close()

        log.debug("BarrierSync epoch=%d sent", self._epoch)

    def _encode_barrier_sync(self) -> bytes:
        rid = _ip_to_int(self.router_id)
        body = struct.pack(
            "!BBH II",
            PKT_BARRIER,
            BARRIER_VERSION,
            BARRIER_SIZE,
            rid,
            self._epoch & 0xFFFF_FFFF,
        )
        chksum = sum(body) & 0xFFFF_FFFF
        return body + struct.pack("!I", chksum)

    # ---- Helpers ------------------------------------------------------------

    def _iface_for_nexthop(self, nexthop_ip: str) -> str:
        """Best-effort: find which local interface reaches this next-hop."""
        nh_parts = nexthop_ip.split(".")
        for iface in self.interfaces:
            my_parts = iface["ip"].split(".")
            if nh_parts[:3] == my_parts[:3]:
                return iface.get("name", "")
        return ""

    def show_routes(self) -> str:
        if not self._current_fib:
            return "No routes installed."
        lines = [f"SBSP routing table ({self.router_id}) — epoch {self._epoch}:"]
        for prefix, r in sorted(self._current_fib.items()):
            lines.append(
                f"  {r.key():<20} via {r.next_hop:<16} metric {r.metric:<8.0f} dev {r.via_iface}"
            )
        return "\n".join(lines)

    def show_stats(self) -> str:
        s = self.stats
        return (
            f"Compute stats:\n"
            f"  Runs:           {s['compute_runs']}\n"
            f"  Routes added:   {s['routes_added']}\n"
            f"  Routes removed: {s['routes_removed']}\n"
            f"  Last run:       {s['last_compute_ms']:.2f} ms\n"
            f"  Epoch:          {self._epoch}"
        )


def _ip_to_int(ip: str) -> int:
    try:
        parts = [int(p) for p in ip.split(".")]
        return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
    except Exception:
        return 0
