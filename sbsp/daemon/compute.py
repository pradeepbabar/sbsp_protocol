"""
sbsp/daemon/compute.py
SBSP Compute Engine — installs both transit routes and prefix (subnet) routes.

Two route types are installed:
  1. Transit routes  — to each router-ID (10.0.0.X/32) via shortest path
  2. Prefix routes   — to each advertised subnet (192.168.X.0/24) via the
                       router that originated the Prefix-LSA, reached via
                       the same shortest-path next-hop
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

SPF_DELAY       = 0.1
PKT_BARRIER     = 0x06
BARRIER_VERSION = 0x01
BARRIER_SIZE    = 16
BARRIER_PORT    = 9992
MCAST_GROUP     = "224.0.0.91"


@dataclass
class Route:
    prefix:    str          # e.g. "10.0.0.3" or "192.168.3.0"
    mask:      int          # prefix length, e.g. 32 or 24
    next_hop:  str
    metric:    float
    via_iface: str = ""
    route_type: str = "transit"   # "transit" | "prefix"

    def cidr(self) -> str:
        return f"{self.prefix}/{self.mask}"

    def __eq__(self, other):
        return (self.prefix == other.prefix and self.mask == other.mask
                and self.next_hop == other.next_hop and self.metric == other.metric)


class KernelFIB:
    def __init__(self):
        self._ipr = None
        try:
            from pyroute2 import IPRoute
            self._ipr = IPRoute()
            log.info("pyroute2 available — Netlink FIB writes enabled")
        except ImportError:
            log.warning("pyroute2 not installed — dry-run mode")

    def add_route(self, route: Route):
        log.info("FIB ADD  %-22s via %-16s metric %.0f [%s]",
                 route.cidr(), route.next_hop, route.metric, route.route_type)
        if self._ipr is None:
            return
        try:
            self._ipr.route("add",
                dst     = route.cidr(),
                gateway = route.next_hop,
                metrics = {"metric": int(route.metric)})
        except Exception:
            try:
                self._ipr.route("replace",
                    dst     = route.cidr(),
                    gateway = route.next_hop,
                    metrics = {"metric": int(route.metric)})
            except Exception as e:
                log.error("FIB add failed for %s: %s", route.cidr(), e)

    def delete_route(self, route: Route):
        log.info("FIB DEL  %s via %s", route.cidr(), route.next_hop)
        if self._ipr is None:
            return
        try:
            self._ipr.route("del", dst=route.cidr(), gateway=route.next_hop)
        except Exception as e:
            log.debug("FIB del skipped %s: %s", route.cidr(), e)

    def close(self):
        if self._ipr:
            self._ipr.close()


class ComputeEngine:
    def __init__(self, lsdb: LSDB, router_id: str, interfaces: List[Dict],
                 fib: Optional[KernelFIB] = None, prefix_lsdb=None):
        self.lsdb        = lsdb
        self.prefix_lsdb = prefix_lsdb   # PrefixLSDB instance, can be None
        self.router_id   = router_id
        self.interfaces  = interfaces
        self.fib         = fib or KernelFIB()
        self._pending    = False
        self._epoch      = 0
        self._current_fib: Dict[str, Route] = {}
        self.stats = {
            "compute_runs": 0, "routes_added": 0,
            "routes_removed": 0, "last_compute_ms": 0.0,
        }

    def schedule_compute(self):
        if not self._pending:
            self._pending = True
            asyncio.get_event_loop().call_later(SPF_DELAY, self._trigger)

    def _trigger(self):
        asyncio.get_event_loop().create_task(self._run_compute())

    async def run(self):
        log.info("Compute engine started for %s", self.router_id)
        while True:
            await asyncio.sleep(3600)
            self.schedule_compute()

    async def _run_compute(self):
        self._pending = False
        t0 = time.monotonic()

        edges = self.lsdb.get_edge_list()
        if not edges:
            log.debug("No edges in LSDB — skipping compute")
            return

        log.info("SBSP compute: %d edges from %s", len(edges), self.router_id)

        # ---- Sorting Barrier SSSP on transit graph -------------------------
        try:
            dist, prev = sorting_barrier_sssp(edges, self.router_id)
        except Exception as e:
            log.error("SSSP failed: %s", e); return

        # ---- Build new route table ----------------------------------------
        new_routes: Dict[str, Route] = {}

        # 1. Transit routes — one /32 per reachable router
        for node, distance in dist.items():
            if node == self.router_id or distance == float("inf"):
                continue
            nh = next_hop(prev, self.router_id, node)
            if nh is None:
                continue
            r = Route(
                prefix     = node,
                mask       = 32,
                next_hop   = nh,
                metric     = distance,
                via_iface  = self._iface_for_nexthop(nh),
                route_type = "transit",
            )
            new_routes[r.cidr()] = r

        # 2. Prefix routes — one /N per advertised subnet
        if self.prefix_lsdb:
            for plsa in self.prefix_lsdb.get_prefix_table():
                if plsa.adv_router == self.router_id:
                    continue    # own subnet, already on lo1

                # Find next-hop toward the advertising router via transit graph
                adv_dist = dist.get(plsa.adv_router, float("inf"))
                if adv_dist == float("inf"):
                    log.debug("Prefix %s unreachable (adv_router %s unreachable)",
                              plsa.network(), plsa.adv_router)
                    continue

                nh = next_hop(prev, self.router_id, plsa.adv_router)
                if nh is None:
                    continue

                total_metric = adv_dist + plsa.metric
                r = Route(
                    prefix     = plsa.prefix,
                    mask       = plsa.prefix_len,
                    next_hop   = nh,
                    metric     = total_metric,
                    via_iface  = self._iface_for_nexthop(nh),
                    route_type = "prefix",
                )
                new_routes[r.cidr()] = r
                log.debug("Prefix route: %s via %s metric %.0f (adv by %s)",
                          r.cidr(), nh, total_metric, plsa.adv_router)

        # ---- Diff and push to FIB -----------------------------------------
        to_add, to_delete = [], []
        for cidr, route in new_routes.items():
            existing = self._current_fib.get(cidr)
            if existing is None or existing != route:
                to_add.append(route)
                if existing:
                    to_delete.append(existing)

        for cidr, route in self._current_fib.items():
            if cidr not in new_routes:
                to_delete.append(route)

        for r in to_delete:
            self.fib.delete_route(r)
        for r in to_add:
            self.fib.add_route(r)

        self._current_fib = new_routes

        transit_count = sum(1 for r in new_routes.values() if r.route_type == "transit")
        prefix_count  = sum(1 for r in new_routes.values() if r.route_type == "prefix")

        elapsed_ms = (time.monotonic() - t0) * 1000
        self.stats["compute_runs"]   += 1
        self.stats["routes_added"]   += len(to_add)
        self.stats["routes_removed"] += len(to_delete)
        self.stats["last_compute_ms"] = elapsed_ms
        self._epoch += 1

        log.info(
            "Compute done %.2fms | epoch=%d | transit=%d prefix=%d (+%d -%d)",
            elapsed_ms, self._epoch,
            transit_count, prefix_count,
            len(to_add), len(to_delete),
        )

        await self._send_barrier_sync()

    async def _send_barrier_sync(self):
        pkt = self._encode_barrier_sync()
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setblocking(False)
        try:
            for iface in self.interfaces:
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                    socket.inet_aton(iface["ip"]))
                    await loop.sock_sendto(sock, pkt, (MCAST_GROUP, BARRIER_PORT))
                except OSError:
                    pass
        finally:
            sock.close()

    def _encode_barrier_sync(self) -> bytes:
        rid  = _ip_to_int(self.router_id)
        body = struct.pack("!BBH II", PKT_BARRIER, BARRIER_VERSION,
                           BARRIER_SIZE, rid, self._epoch & 0xFFFFFFFF)
        chksum = sum(body) & 0xFFFFFFFF
        return body + struct.pack("!I", chksum)

    def _iface_for_nexthop(self, nexthop_ip: str) -> str:
        nh_parts = nexthop_ip.split(".")
        for iface in self.interfaces:
            my_parts = iface["ip"].split(".")
            if nh_parts[:3] == my_parts[:3]:
                return iface.get("name", "")
        return ""

    def show_routes(self) -> str:
        if not self._current_fib:
            return "No routes installed."
        lines = [f"SBSP route table ({self.router_id}) — epoch {self._epoch}:"]
        lines.append(f"  {'Prefix':<24} {'Via':<18} {'Metric':<8} {'Type':<10} {'Dev'}")
        lines.append("  " + "-" * 70)
        for cidr, r in sorted(self._current_fib.items()):
            lines.append(f"  {r.cidr():<24} {r.next_hop:<18} {r.metric:<8.0f} "
                         f"{r.route_type:<10} {r.via_iface}")
        return "\n".join(lines)

    def show_stats(self) -> str:
        s = self.stats
        return (f"Compute stats:\n"
                f"  Runs:         {s['compute_runs']}\n"
                f"  Routes added: {s['routes_added']}\n"
                f"  Routes del:   {s['routes_removed']}\n"
                f"  Last run:     {s['last_compute_ms']:.2f} ms\n"
                f"  Epoch:        {self._epoch}")


def _ip_to_int(ip: str) -> int:
    try:
        parts = [int(p) for p in ip.split(".")]
        return (parts[0]<<24)|(parts[1]<<16)|(parts[2]<<8)|parts[3]
    except Exception:
        return 0