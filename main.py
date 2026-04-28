"""
sbsp/daemon/main.py
SBSP daemon entry point.

Usage:
  python -m sbsp.daemon.main --config /etc/sbsp/sbsp.conf
  python -m sbsp.daemon.main --router-id 10.0.0.1 --area 0.0.0.0 --interfaces eth0:10.0.0.1:10,eth1:10.0.1.1:5

Config file (INI format):
  [sbsp]
  router_id  = 10.0.0.1
  area_id    = 0.0.0.0
  log_level  = INFO

  [interfaces]
  eth0 = 10.0.0.1/24  cost=10
  eth1 = 10.0.1.1/24  cost=5
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import logging
import os
import signal
import sys
from typing import List

# Allow running as `python -m sbsp.daemon.main` from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from sbsp.daemon.hello   import HelloProtocol
from sbsp.daemon.lsdb    import LSDB
from sbsp.daemon.compute import ComputeEngine, KernelFIB

log = logging.getLogger("sbsp")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def parse_iface_arg(spec: str) -> dict:
    """'eth0:10.0.0.1:10' -> {'name':'eth0','ip':'10.0.0.1','cost':10}"""
    parts = spec.split(":")
    return {
        "name": parts[0],
        "ip":   parts[1] if len(parts) > 1 else "0.0.0.0",
        "cost": int(parts[2]) if len(parts) > 2 else 10,
    }


def load_config_file(path: str) -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(path)
    result: dict = {}

    sbsp = cfg["sbsp"] if "sbsp" in cfg else {}
    result["router_id"] = sbsp.get("router_id", "0.0.0.0")
    result["area_id"]   = sbsp.get("area_id",   "0.0.0.0")
    result["log_level"] = sbsp.get("log_level",  "INFO")

    interfaces = []
    if "interfaces" in cfg:
        for name, val in cfg["interfaces"].items():
            # val format: "10.0.0.1/24  cost=10"
            parts = val.split()
            ip = parts[0].split("/")[0]
            cost = 10
            for p in parts[1:]:
                if p.startswith("cost="):
                    cost = int(p.split("=")[1])
            interfaces.append({"name": name, "ip": ip, "cost": cost})
    result["interfaces"] = interfaces
    return result


def auto_detect_router_id() -> str:
    """Use first non-loopback IPv4 address as router-ID."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def auto_detect_interfaces() -> List[dict]:
    """Read /proc/net/if_inet6 and /proc/net/fib_trie — simple heuristic."""
    ifaces = []
    try:
        import socket
        import fcntl
        import struct
        SIOCGIFADDR = 0x8915
        with open("/proc/net/dev") as f:
            for line in f:
                name = line.split(":")[0].strip()
                if name in ("lo", "") or not name:
                    continue
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    ifreq = struct.pack("256s", name[:15].encode())
                    res = fcntl.ioctl(s.fileno(), SIOCGIFADDR, ifreq)
                    ip = socket.inet_ntoa(res[20:24])
                    s.close()
                    if ip.startswith("127."):
                        continue
                    ifaces.append({"name": name, "ip": ip, "cost": 10})
                except Exception:
                    continue
    except Exception:
        pass
    return ifaces or [{"name": "eth0", "ip": "10.0.0.1", "cost": 10}]


# ---------------------------------------------------------------------------
# SBSP Daemon
# ---------------------------------------------------------------------------

class SbspDaemon:
    def __init__(self, router_id: str, area_id: str, interfaces: List[dict]):
        self.router_id  = router_id
        self.area_id    = area_id
        self.interfaces = interfaces

        self.lsdb    = LSDB(router_id=router_id)
        self.fib     = KernelFIB()
        self.compute = ComputeEngine(
            lsdb       = self.lsdb,
            router_id  = router_id,
            interfaces = interfaces,
            fib        = self.fib,
        )
        self.hello = HelloProtocol(
            router_id         = router_id,
            area_id           = area_id,
            interfaces        = interfaces,
            on_neighbour_up   = self._on_neighbour_up,
            on_neighbour_down = self._on_neighbour_down,
            on_full           = self._on_adjacency_full,
        )

        # Wire LSDB change -> compute
        self.lsdb.register_change_callback(self.compute.schedule_compute)

    # ---- Adjacency callbacks ------------------------------------------------

    def _on_neighbour_up(self, nbr):
        log.info("Neighbour UP: %s on %s cost=%d", nbr.router_id, nbr.interface, nbr.cost)

    def _on_neighbour_down(self, nbr):
        log.warning("Neighbour DOWN: %s — withdrawing their LSAs", nbr.router_id)
        # Age out all LSAs from this neighbour
        asyncio.get_event_loop().create_task(
            self._withdraw_neighbour_lsas(nbr.router_id)
        )

    def _on_adjacency_full(self, nbr):
        log.info("Adjacency FULL with %s — originating link LSA", nbr.router_id)
        # Originate our LSA for this link
        asyncio.get_event_loop().create_task(
            self.lsdb.originate_lsa(
                link_id  = nbr.router_id,
                fwd_cost = nbr.cost,
                rev_cost = nbr.cost,   # assume symmetric; override with config
            )
        )

    async def _withdraw_neighbour_lsas(self, dead_router_id: str):
        """Remove all LSAs originated by a dead neighbour."""
        to_remove = [
            lsa for lsa in self.lsdb.get_all_lsas()
            if lsa.adv_router == dead_router_id
        ]
        for lsa in to_remove:
            await self.lsdb.withdraw_lsa(lsa.link_id)
        if to_remove:
            log.info("Removed %d LSAs from dead neighbour %s",
                     len(to_remove), dead_router_id)

    # ---- Run ----------------------------------------------------------------

    async def start(self):
        log.info("=" * 60)
        log.info("SBSP daemon starting")
        log.info("  Router-ID : %s", self.router_id)
        log.info("  Area      : %s", self.area_id)
        log.info("  Interfaces: %s", [i["name"] for i in self.interfaces])
        log.info("=" * 60)

        # Graceful shutdown on SIGTERM / SIGINT
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown)

        await asyncio.gather(
            self.hello.run(),
            self.lsdb.flood_loop(),
            self.lsdb.aging_loop(),
            self.lsdb.refresh_loop(),
            self.compute.run(),
        )

    def _shutdown(self):
        log.info("SBSP daemon shutting down")
        self.fib.close()
        asyncio.get_event_loop().stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="SBSP routing daemon")
    parser.add_argument("--config",     help="Path to sbsp.conf")
    parser.add_argument("--router-id",  help="Router ID (IPv4 dotted-quad)")
    parser.add_argument("--area",       default="0.0.0.0", help="Area ID")
    parser.add_argument("--interfaces", help="Comma-separated iface:ip:cost list")
    parser.add_argument("--log-level",  default="INFO")
    args = parser.parse_args()

    # Load config
    if args.config:
        cfg = load_config_file(args.config)
    else:
        cfg = {}

    router_id  = args.router_id  or cfg.get("router_id")  or auto_detect_router_id()
    area_id    = args.area        or cfg.get("area_id",  "0.0.0.0")
    log_level  = args.log_level   or cfg.get("log_level", "INFO")

    if args.interfaces:
        interfaces = [parse_iface_arg(s) for s in args.interfaces.split(",")]
    elif cfg.get("interfaces"):
        interfaces = cfg["interfaces"]
    else:
        interfaces = auto_detect_interfaces()

    logging.basicConfig(
        level   = getattr(logging, log_level.upper(), logging.INFO),
        format  = "%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
        datefmt = "%H:%M:%S",
    )

    daemon = SbspDaemon(router_id, area_id, interfaces)
    asyncio.run(daemon.start())


if __name__ == "__main__":
    main()
