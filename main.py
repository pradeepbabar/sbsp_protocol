"""
sbsp/daemon/main.py
SBSP daemon entry point — now with subnet advertisement.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from sbsp.daemon.hello     import HelloProtocol
from sbsp.daemon.lsdb      import LSDB
from sbsp.daemon.compute   import ComputeEngine, KernelFIB
from sbsp.daemon.advertise import PrefixLSDB

log = logging.getLogger("sbsp")


def parse_iface_arg(spec: str) -> dict:
    parts = spec.split(":")
    return {
        "name": parts[0],
        "ip":   parts[1] if len(parts) > 1 else "0.0.0.0",
        "cost": int(parts[2]) if len(parts) > 2 else 10,
    }

def parse_prefix(prefix_str: str):
    """'192.168.1.0/24' -> ('192.168.1.0', 24)"""
    if not prefix_str:
        return None, None
    try:
        net, mask = prefix_str.split("/")
        return net.strip(), int(mask.strip())
    except Exception:
        return None, None

def auto_detect_router_id() -> str:
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
    ifaces = []
    try:
        import socket, fcntl, struct
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
    return ifaces or [{"name": "eth1", "ip": "10.1.1.1", "cost": 10}]


# ---------------------------------------------------------------------------
# Main Daemon
# ---------------------------------------------------------------------------

class SbspDaemon:
    def __init__(
        self,
        router_id:  str,
        area_id:    str,
        interfaces: List[dict],
        loopback_prefix: Optional[str] = None,   # e.g. "192.168.1.0"
        loopback_len:    Optional[int]  = None,   # e.g. 24
    ):
        self.router_id        = router_id
        self.area_id          = area_id
        self.interfaces       = interfaces
        self.loopback_prefix  = loopback_prefix
        self.loopback_len     = loopback_len

        # Core components
        self.lsdb        = LSDB(router_id=router_id)
        self.prefix_lsdb = PrefixLSDB(router_id=router_id)
        self.fib         = KernelFIB()
        self.compute     = ComputeEngine(
            lsdb        = self.lsdb,
            router_id   = router_id,
            interfaces  = interfaces,
            fib         = self.fib,
            prefix_lsdb = self.prefix_lsdb,
        )
        self.hello = HelloProtocol(
            router_id          = router_id,
            area_id            = area_id,
            interfaces         = interfaces,
            on_neighbour_up    = self._on_neighbour_up,
            on_neighbour_down  = self._on_neighbour_down,
            on_full            = self._on_adjacency_full,
        )

        # Any change in either LSDB triggers a recompute
        self.lsdb.register_change_callback(self.compute.schedule_compute)
        self.prefix_lsdb.register_change_callback(self.compute.schedule_compute)

    # ---- Adjacency callbacks -----------------------------------------------

    def _on_neighbour_up(self, nbr):
        log.info("Neighbour UP: %s on %s cost=%d", nbr.router_id, nbr.interface, nbr.cost)

    def _on_neighbour_down(self, nbr):
        log.warning("Neighbour DOWN: %s — withdrawing LSAs", nbr.router_id)
        asyncio.get_event_loop().create_task(
            self._withdraw_neighbour_lsas(nbr.router_id)
        )

    def _on_adjacency_full(self, nbr):
        log.info("Adjacency FULL with %s — originating link LSA", nbr.router_id)
        asyncio.get_event_loop().create_task(
            self.lsdb.originate_lsa(
                link_id  = nbr.router_id,
                fwd_cost = nbr.cost,
                rev_cost = nbr.cost,
            )
        )

    async def _withdraw_neighbour_lsas(self, dead_router_id: str):
        to_remove = [l for l in self.lsdb.get_all_lsas()
                     if l.adv_router == dead_router_id]
        for lsa in to_remove:
            await self.lsdb.withdraw_lsa(lsa.link_id)
        if to_remove:
            log.info("Removed %d LSAs from %s", len(to_remove), dead_router_id)

    # ---- Startup -----------------------------------------------------------

    async def _originate_own_prefix(self):
        """Advertise our own loopback subnet to the area."""
        if not self.loopback_prefix or not self.loopback_len:
            log.info("No loopback prefix configured — skipping prefix advertisement")
            return
        log.info("Originating prefix LSA: %s/%d",
                 self.loopback_prefix, self.loopback_len)
        await self.prefix_lsdb.originate(
            prefix     = self.loopback_prefix,
            prefix_len = self.loopback_len,
            metric     = 0,   # cost to reach own subnet = 0
        )

    async def start(self):
        log.info("=" * 60)
        log.info("SBSP daemon starting")
        log.info("  Router-ID  : %s", self.router_id)
        log.info("  Area       : %s", self.area_id)
        log.info("  Interfaces : %s", [i["name"] for i in self.interfaces])
        if self.loopback_prefix:
            log.info("  Loopback   : %s/%s", self.loopback_prefix, self.loopback_len)
        log.info("=" * 60)

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown)

        # Originate our prefix LSA shortly after start
        loop.call_later(5.0, lambda: asyncio.ensure_future(
            self._originate_own_prefix()
        ))

        await asyncio.gather(
            self.hello.run(),
            self.lsdb.flood_loop(),
            self.lsdb.aging_loop(),
            self.lsdb.refresh_loop(),
            self.prefix_lsdb.aging_loop(),
            self.prefix_lsdb.flood_loop(),
            self.prefix_lsdb.refresh_loop(),
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
    parser.add_argument("--router-id",  help="Router ID (IPv4 dotted-quad)")
    parser.add_argument("--area",       default="0.0.0.0")
    parser.add_argument("--interfaces", help="Comma-separated iface:ip:cost")
    parser.add_argument("--loopback",   default="",
                        help="Own subnet to advertise, e.g. 192.168.1.0/24")
    parser.add_argument("--log-level",  default="INFO")
    args = parser.parse_args()

    router_id  = args.router_id  or auto_detect_router_id()
    area_id    = args.area
    interfaces = ([parse_iface_arg(s) for s in args.interfaces.split(",")]
                  if args.interfaces else auto_detect_interfaces())

    loopback_prefix, loopback_len = parse_prefix(args.loopback)

    logging.basicConfig(
        level   = getattr(logging, args.log_level.upper(), logging.INFO),
        format  = "%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
        datefmt = "%H:%M:%S",
    )

    daemon = SbspDaemon(
        router_id        = router_id,
        area_id          = area_id,
        interfaces       = interfaces,
        loopback_prefix  = loopback_prefix,
        loopback_len     = loopback_len,
    )
    asyncio.run(daemon.start())


if __name__ == "__main__":
    main()