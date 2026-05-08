"""
sbsp/daemon/hello.py
SBSP Hello — neighbour discovery and adjacency management.

Hello packet wire format (32 bytes, big-endian):
  Offset  Len  Field
  ------  ---  -----
   0       1   Packet type   (0x01 = Hello)
   1       1   Version       (0x01)
   2       2   Length        (u16, total bytes)
   4       4   Router ID     (u32, dotted-quad packed)
   8       4   Area ID       (u32)
  12       4   Interface IP  (u32)
  16       4   Interface cost (u32)
  20       2   Hello interval (u16, seconds)
  22       2   Dead interval  (u16, seconds)
  24       2   Topo rank hint (u16)
  26       2   Flags          (u16; bit0=DR_eligible)
  28       4   Checksum       (u32 — simple sum mod 2^32)
  Total: 32 bytes

Adjacency lifecycle:
  DOWN -> INIT (Hello received) -> 2WAY (own ID in their Hello)
       -> EXSTART -> EXCHANGE -> LOADING -> FULL
  (Exchange/Loading are handled by lsdb.py flood_loop)
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

# Wire constants
PKT_HELLO       = 0x01
HELLO_VERSION   = 0x01
HELLO_SIZE      = 32
MCAST_GROUP     = "224.0.0.91"    # SBSP-specific multicast (not 224.0.0.5/OSPF)
MCAST_PORT      = 9991
HELLO_INTERVAL  = 10              # seconds
DEAD_INTERVAL   = 40              # seconds
HELLO_FMT       = "!BBH IIII HH HH I"


class AdjState(Enum):
    DOWN      = auto()
    INIT      = auto()
    TWO_WAY   = auto()
    EXSTART   = auto()
    EXCHANGE  = auto()
    LOADING   = auto()
    FULL      = auto()


# ---------------------------------------------------------------------------
# Neighbour
# ---------------------------------------------------------------------------

@dataclass
class Neighbour:
    router_id:   str
    address:     str              # source IP of their Hello packets
    interface:   str              # our interface they arrived on
    cost:        int              # their reported interface cost
    topo_rank:   int
    state:       AdjState = AdjState.DOWN
    last_hello:  float = field(default_factory=time.monotonic)

    def is_dead(self) -> bool:
        return (time.monotonic() - self.last_hello) > DEAD_INTERVAL

    def touch(self):
        self.last_hello = time.monotonic()

    def __repr__(self):
        return f"Nbr({self.router_id} via {self.interface} state={self.state.name})"


# ---------------------------------------------------------------------------
# Hello wire encode / decode
# ---------------------------------------------------------------------------

def encode_hello(
    router_id: str,
    area_id: str,
    iface_ip: str,
    iface_cost: int,
    topo_rank: int = 0,
    dr_eligible: bool = True,
) -> bytes:
    rid   = _ip_to_int(router_id)
    aid   = _ip_to_int(area_id)
    ifip  = _ip_to_int(iface_ip)
    flags = 0x0001 if dr_eligible else 0x0000

    body = struct.pack(
        "!BBH IIII HH HH",
        PKT_HELLO,
        HELLO_VERSION,
        HELLO_SIZE,
        rid,
        aid,
        ifip,
        iface_cost,
        HELLO_INTERVAL,
        DEAD_INTERVAL,
        topo_rank & 0xFFFF,
        flags,
    )
    chksum = sum(body) & 0xFFFF_FFFF
    return body + struct.pack("!I", chksum)


def decode_hello(data: bytes) -> Optional[dict]:
    if len(data) < HELLO_SIZE:
        return None
    try:
        (pkt_type, version, length,
         rid, aid, ifip, cost,
         hello_int, dead_int,
         topo_rank, flags,
         chksum) = struct.unpack("!BBH IIII HH HH I", data[:HELLO_SIZE])
    except struct.error:
        return None

    if pkt_type != PKT_HELLO or version != HELLO_VERSION:
        return None

    # Verify checksum
    expected = sum(data[:28]) & 0xFFFF_FFFF
    if expected != chksum:
        log.warning("Hello checksum mismatch from %s", _int_to_ip(rid))
        return None

    return {
        "router_id":     _int_to_ip(rid),
        "area_id":       _int_to_ip(aid),
        "iface_ip":      _int_to_ip(ifip),
        "cost":          cost,
        "hello_interval": hello_int,
        "dead_interval":  dead_int,
        "topo_rank":     topo_rank,
        "dr_eligible":   bool(flags & 0x0001),
    }


# ---------------------------------------------------------------------------
# Hello Protocol
# ---------------------------------------------------------------------------

class HelloProtocol:
    """
    Sends Hello packets on all SBSP-enabled interfaces via multicast.
    Maintains neighbour table and fires callbacks on adjacency changes.
    """

    def __init__(
        self,
        router_id:        str,
        area_id:          str,
        interfaces:       List[Dict],        # [{"name":"eth0","ip":"10.0.0.1","cost":10}, ...]
        on_neighbour_up:  Optional[Callable] = None,   # cb(neighbour: Neighbour)
        on_neighbour_down: Optional[Callable] = None,  # cb(neighbour: Neighbour)
        on_full:          Optional[Callable] = None,   # cb(neighbour: Neighbour)
    ):
        self.router_id    = router_id
        self.area_id      = area_id
        self.interfaces   = interfaces
        self.neighbours: Dict[str, Neighbour] = {}   # router_id -> Neighbour
        self._on_up       = on_neighbour_up
        self._on_down     = on_neighbour_down
        self._on_full     = on_full
        self._transport   = None
        self._protocol    = None

    # ---- Main entry point ---------------------------------------------------

    async def run(self):
        """Start Hello send loop, receive loop, and dead-check loop."""
        await asyncio.gather(
            self._send_loop(),
            self._recv_loop(),
            self._dead_check_loop(),
        )

    # ---- Send ---------------------------------------------------------------

    async def _send_loop(self):
        while True:
            for iface in self.interfaces:
                pkt = encode_hello(
                    router_id  = self.router_id,
                    area_id    = self.area_id,
                    iface_ip   = iface["ip"],
                    iface_cost = iface.get("cost", 10),
                )
                await self._multicast_send(pkt, iface)
                log.debug("Hello sent on %s (%s)", iface["name"], iface["ip"])
            await asyncio.sleep(HELLO_INTERVAL)

    async def _multicast_send(self, data: bytes, iface: dict):
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        try:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(iface["ip"]),
            )
        except OSError:
            pass   # interface may not be up yet; skip silently
        sock.setblocking(False)
        try:
            await loop.sock_sendto(sock, data, (MCAST_GROUP, MCAST_PORT))
        except OSError as e:
            log.debug("Hello send error on %s: %s", iface["name"], e)
        finally:
            sock.close()

    # ---- Receive ------------------------------------------------------------

    async def _recv_loop(self):
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        sock.bind(("", MCAST_PORT))

        # Join multicast group on each interface
        for iface in self.interfaces:
            mreq = struct.pack(
                "4s4s",
                socket.inet_aton(MCAST_GROUP),
                socket.inet_aton(iface["ip"]),
            )
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                log.info("Joined SBSP multicast on %s (%s)", iface["name"], iface["ip"])
            except OSError as e:
                log.warning("Multicast join failed on %s: %s", iface["name"], e)

        sock.setblocking(False)
        log.info("Hello receiver listening on %s:%d", MCAST_GROUP, MCAST_PORT)

        while True:
            try:
                data, (src_ip, _) = await loop.sock_recvfrom(sock, 4096)
                await self._handle_hello(data, src_ip)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("Recv error: %s", e)

    async def _handle_hello(self, data: bytes, src_ip: str):
        hello = decode_hello(data)
        if hello is None:
            return

        rid = hello["router_id"]
        if rid == self.router_id:
            return   # own Hello reflected back; ignore

        nbr = self.neighbours.get(rid)
        if nbr is None:
            # New neighbour
            iface_name = self._iface_for_ip(src_ip) or "unknown"
            nbr = Neighbour(
                router_id = rid,
                address   = src_ip,
                interface = iface_name,
                cost      = hello["cost"],
                topo_rank = hello["topo_rank"],
                state     = AdjState.INIT,
            )
            self.neighbours[rid] = nbr
            log.info("New neighbour discovered: %s on %s", rid, iface_name)
            if self._on_up:
                self._on_up(nbr)

        nbr.touch()
        nbr.cost      = hello["cost"]
        nbr.topo_rank = hello["topo_rank"]

        # Advance state machine
        old_state = nbr.state
        if nbr.state == AdjState.INIT:
            nbr.state = AdjState.TWO_WAY
            log.info("Adjacency 2-Way with %s", rid)
        elif nbr.state == AdjState.TWO_WAY:
            # Simplified: go straight to FULL (no DBD exchange in this MVP)
            # Full implementation would go EXSTART -> EXCHANGE -> LOADING -> FULL
            nbr.state = AdjState.FULL
            log.info("Adjacency FULL with %s", rid)
            if self._on_full:
                self._on_full(nbr)

        if old_state != nbr.state:
            log.debug("Nbr %s: %s -> %s", rid, old_state.name, nbr.state.name)

    # ---- Dead check ---------------------------------------------------------

    async def _dead_check_loop(self):
        while True:
            await asyncio.sleep(DEAD_INTERVAL / 4)
            dead = [nbr for nbr in self.neighbours.values() if nbr.is_dead()]
            for nbr in dead:
                log.warning("Neighbour DEAD: %s (last seen %.0fs ago)",
                            nbr.router_id,
                            time.monotonic() - nbr.last_hello)
                nbr.state = AdjState.DOWN
                del self.neighbours[nbr.router_id]
                if self._on_down:
                    self._on_down(nbr)

    # ---- Helpers ------------------------------------------------------------

    def _iface_for_ip(self, src_ip: str) -> Optional[str]:
        """Find which of our interfaces is on the same subnet as src_ip."""
        src_parts = src_ip.split(".")
        for iface in self.interfaces:
            my_parts = iface["ip"].split(".")
            if src_parts[:3] == my_parts[:3]:   # /24 heuristic
                return iface["name"]
        return None

    def get_neighbours(self) -> List[Neighbour]:
        return list(self.neighbours.values())

    def full_neighbours(self) -> List[Neighbour]:
        return [n for n in self.neighbours.values() if n.state == AdjState.FULL]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ip_to_int(ip: str) -> int:
    try:
        parts = [int(p) for p in ip.split(".")]
        return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
    except Exception:
        return 0


def _int_to_ip(n: int) -> str:
    return f"{(n>>24)&0xFF}.{(n>>16)&0xFF}.{(n>>8)&0xFF}.{n&0xFF}"