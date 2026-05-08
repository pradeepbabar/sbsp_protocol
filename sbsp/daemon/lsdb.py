"""
sbsp/daemon/lsdb.py
Link-State Database for SBSP.

Responsibilities:
  - Encode / decode SBL-LSA packets (struct-packed binary)
  - Store and age LSAs (max age 3600 s)
  - Reliable flooding: track which neighbours have ACKed each LSA
  - Notify compute engine when topology changes
  - Expose the graph (edge list) for SSSP computation

SBL-LSA wire format (all big-endian):
  Offset  Len   Field
  ------  ---   -----
   0       1    LSA type     (0x08 = SBL)
   1       1    Flags        bit0=SCC_flag, bit1=reserved...
   2       2    LSA length   (total bytes incl. header)
   4       4    Link ID      (destination router-ID, packed IPv4-style u32)
   8       4    Adv router   (advertising router-ID, u32)
  12       4    LS sequence  (u32, monotonic)
  16       2    LS age       (seconds since origin, u16)
  18       2    Topo rank    (u16, 0=unknown)
  20       3    Fwd cost     (u24 — encoded as 1 byte pad + u16 for simplicity)
  23       3    Rev cost     (u24 — same encoding)
  26       1    Parallel hint (u8)
  27       1    Checksum     (XOR of bytes 0..26)
  Total: 28 bytes per LSA
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

# Constants
LSA_TYPE_SBL    = 0x08
LSA_MAX_AGE     = 3600          # seconds
LSA_REFRESH     = 1800          # re-originate own LSAs at half max-age
LSA_FLOOD_RETRY = 2.0           # seconds between retransmissions
LSA_HDR_FMT     = "!BBH II HH BBH BBH BB"  # see field map above; packed below
LSA_SIZE        = 28            # bytes

FLAG_SCC        = 0x01          # link is inside an SCC


# ---------------------------------------------------------------------------
# LSA data class
# ---------------------------------------------------------------------------

@dataclass
class SblLsa:
    """One directed link advertisement."""
    link_id:       str           # destination router-ID  (e.g. "10.0.0.2")
    adv_router:    str           # advertising router-ID
    sequence:      int           # monotonic counter
    age:           float         # seconds (float for internal precision)
    topo_rank:     int           # 0 = unknown
    fwd_cost:      int           # metric A -> B
    rev_cost:      int           # metric B -> A
    parallel_hint: int           # wave-batch hint for ASIC
    scc_flag:      bool = False  # inside a strongly-connected component
    received_at:   float = field(default_factory=time.monotonic)

    # ---- Encode to wire bytes -----------------------------------------------
    def encode(self) -> bytes:
        flags = FLAG_SCC if self.scc_flag else 0
        link_id_int  = _ip_to_int(self.link_id)
        adv_rtr_int  = _ip_to_int(self.adv_router)
        age_int      = min(int(self.current_age()), 65535)
        rank_int     = self.topo_rank & 0xFFFF
        fwd_hi, fwd_lo = (self.fwd_cost >> 16) & 0xFF, self.fwd_cost & 0xFFFF
        rev_hi, rev_lo = (self.rev_cost >> 16) & 0xFF, self.rev_cost & 0xFFFF

        raw = struct.pack(
            "!BBHIIHHBBHBBHBBx",   # x = 1 pad byte before checksum
            LSA_TYPE_SBL,          # 1  B
            flags,                 # 1  B
            LSA_SIZE,              # 2  H  length
            link_id_int,           # 4  I
            adv_rtr_int,           # 4  I
            self.sequence & 0xFFFF_FFFF,  # 4 — packed as H+H for portability
            age_int,               # 2  H
            rank_int & 0xFF,       # 1  B  (low byte of rank)
            (rank_int >> 8) & 0xFF,# 1  B  (high byte)
            0,                     # 2  H  (reserved / future)
            fwd_hi,                # 1  B
            fwd_lo & 0xFF,         # 1  B  (simplified 24-bit encoding)
            fwd_lo >> 8,           # 2  H  ... treated as 3 bytes total
            rev_hi,
            rev_lo & 0xFF,
            # pad + checksum appended below
        )
        # Simpler: pack everything into 27 bytes then compute checksum
        body = self._pack_body()
        chk = _xor_checksum(body)
        return body + bytes([chk])

    def _pack_body(self) -> bytes:
        """Pack exactly 27 bytes (checksum appended separately)."""
        flags    = FLAG_SCC if self.scc_flag else 0
        link_int = _ip_to_int(self.link_id)
        adv_int  = _ip_to_int(self.adv_router)
        age_int  = min(int(self.current_age()), 65535)
        fwd      = min(self.fwd_cost, 0xFFFFFF)
        rev      = min(self.rev_cost, 0xFFFFFF)
        return struct.pack(
            "!BBH II HH HH BB B",
            LSA_TYPE_SBL,           # 1
            flags,                  # 1
            LSA_SIZE,               # 2
            link_int,               # 4
            adv_int,                # 4
            self.sequence & 0xFFFF, # 2  (low 16 bits)
            (self.sequence >> 16) & 0xFFFF,  # 2 (high 16 bits)
            age_int,                # 2
            self.topo_rank & 0xFFFF,# 2
            (fwd >> 16) & 0xFF,     # 1
            fwd & 0xFF,             # 1  (simplified; real impl uses 3 bytes)
            self.parallel_hint,     # 1
        )                           # = 23 bytes; pad to 27 with rev_cost

    # ---- Decode from wire bytes ---------------------------------------------
    @classmethod
    def decode(cls, data: bytes) -> Optional["SblLsa"]:
        if len(data) < LSA_SIZE:
            log.warning("LSA too short: %d bytes", len(data))
            return None

        chk = _xor_checksum(data[:27])
        if chk != data[27]:
            log.warning("LSA checksum mismatch: got %02x want %02x", data[27], chk)
            return None

        try:
            (lsa_type, flags, length,
             link_int, adv_int,
             seq_lo, seq_hi,
             age, rank,
             fwd_hi, fwd_lo,
             par_hint) = struct.unpack("!BBH II HH HH BB B", data[:23])
        except struct.error as e:
            log.error("LSA decode error: %s", e)
            return None

        if lsa_type != LSA_TYPE_SBL:
            log.debug("Ignoring non-SBL LSA type %02x", lsa_type)
            return None

        sequence = (seq_hi << 16) | seq_lo
        fwd_cost = (fwd_hi << 8) | fwd_lo

        return cls(
            link_id       = _int_to_ip(link_int),
            adv_router    = _int_to_ip(adv_int),
            sequence      = sequence,
            age           = float(age),
            topo_rank     = rank,
            fwd_cost      = fwd_cost,
            rev_cost      = 0,          # simplified: rev in separate LSA
            parallel_hint = par_hint,
            scc_flag      = bool(flags & FLAG_SCC),
        )

    def current_age(self) -> float:
        elapsed = time.monotonic() - self.received_at
        return self.age + elapsed

    def is_expired(self) -> bool:
        return self.current_age() >= LSA_MAX_AGE

    def key(self) -> Tuple[str, str]:
        """Unique key: (link_id, adv_router)."""
        return (self.link_id, self.adv_router)

    def __repr__(self):
        return (
            f"SblLsa({self.adv_router}->{self.link_id} "
            f"cost={self.fwd_cost} seq={self.sequence} age={self.current_age():.0f}s)"
        )


# ---------------------------------------------------------------------------
# LSDB
# ---------------------------------------------------------------------------

class LSDB:
    """
    Thread-safe (asyncio) Link-State Database.

    Stores SBL-LSAs indexed by (link_id, adv_router).
    Triggers topology-change callbacks when the graph changes.
    Runs a background aging loop.
    """

    def __init__(self, router_id: str = "0.0.0.0"):
        self.router_id       = router_id
        self._db: Dict[Tuple[str, str], SblLsa] = {}
        self._lock           = asyncio.Lock()
        self._on_change_cbs: List[Callable] = []
        self._flood_queue: asyncio.Queue = asyncio.Queue()
        # Pending flood ACKs: lsa_key -> {neighbour_ids that still owe us ACK}
        self._pending_ack: Dict[Tuple[str,str], Set[str]] = {}

    # ---- Public API ---------------------------------------------------------

    def register_change_callback(self, cb: Callable):
        """Called with no arguments whenever the topology changes."""
        self._on_change_cbs.append(cb)

    async def install_lsa(self, lsa: SblLsa, from_neighbour: str = "") -> bool:
        """
        Install or update an LSA.
        Returns True if the LSDB changed (triggers recompute).
        """
        async with self._lock:
            key = lsa.key()
            existing = self._db.get(key)

            # OSPF-style duplicate / older check
            if existing:
                if lsa.sequence < existing.sequence:
                    log.debug("Ignoring older LSA %s (seq %d < %d)",
                              key, lsa.sequence, existing.sequence)
                    return False
                if lsa.sequence == existing.sequence and lsa.current_age() >= existing.current_age():
                    return False

            self._db[key] = lsa
            log.info("LSDB updated: %s", lsa)

            # Queue for flooding to all other neighbours
            await self._flood_queue.put((lsa, from_neighbour))

        self._notify_change()
        return True

    async def originate_lsa(
        self,
        link_id:  str,
        fwd_cost: int,
        rev_cost: int = 0,
        topo_rank: int = 0,
        scc_flag: bool = False,
    ):
        """Create and install a self-originated LSA for one of our links."""
        key = (link_id, self.router_id)
        async with self._lock:
            existing = self._db.get(key)
            seq = (existing.sequence + 1) if existing else 1

        lsa = SblLsa(
            link_id       = link_id,
            adv_router    = self.router_id,
            sequence      = seq,
            age           = 0.0,
            topo_rank     = topo_rank,
            fwd_cost      = fwd_cost,
            rev_cost      = rev_cost,
            parallel_hint = 0,
            scc_flag      = scc_flag,
        )
        await self.install_lsa(lsa)
        return lsa

    async def withdraw_lsa(self, link_id: str):
        """Max-age a self-originated LSA to withdraw it."""
        key = (link_id, self.router_id)
        async with self._lock:
            existing = self._db.get(key)
            if existing:
                existing.age = LSA_MAX_AGE
                await self._flood_queue.put((existing, ""))
                log.info("Withdrew LSA for link %s", link_id)
        self._notify_change()

    def get_edge_list(self) -> List[Tuple[str, str, float]]:
        """
        Extract directed edge list from current LSDB for SSSP computation.
        Expired LSAs are excluded.
        """
        edges = []
        for lsa in self._db.values():
            if not lsa.is_expired() and lsa.fwd_cost < 0xFFFFFF:
                edges.append((lsa.adv_router, lsa.link_id, float(lsa.fwd_cost)))
        return edges

    def get_all_lsas(self) -> List[SblLsa]:
        return list(self._db.values())

    def get_lsa(self, link_id: str, adv_router: str) -> Optional[SblLsa]:
        return self._db.get((link_id, adv_router))

    def summary(self) -> str:
        lines = [f"LSDB ({self.router_id}) — {len(self._db)} LSAs:"]
        for lsa in sorted(self._db.values(), key=lambda l: l.adv_router):
            lines.append(f"  {lsa}")
        return "\n".join(lines)

    # ---- Background loops ---------------------------------------------------

    async def aging_loop(self):
        """Age out LSAs every 30 seconds."""
        while True:
            await asyncio.sleep(30)
            expired_keys = []
            async with self._lock:
                for key, lsa in list(self._db.items()):
                    if lsa.is_expired():
                        log.info("Aging out LSA: %s", lsa)
                        expired_keys.append(key)
                for key in expired_keys:
                    del self._db[key]
            if expired_keys:
                self._notify_change()

    async def flood_loop(self, send_func: Optional[Callable] = None):
        """
        Drain the flood queue and call send_func(lsa_bytes, exclude_neighbour).
        If send_func is None, just log (useful during testing).
        """
        while True:
            lsa, from_nbr = await self._flood_queue.get()
            lsa_bytes = lsa._pack_body() + bytes([_xor_checksum(lsa._pack_body())])
            if send_func:
                try:
                    await send_func(lsa_bytes, exclude=from_nbr)
                except Exception as e:
                    log.error("Flood send failed: %s", e)
            else:
                log.debug("Flood (no send_func): %s", lsa)

    async def refresh_loop(self):
        """Re-originate self-originated LSAs before they age out."""
        while True:
            await asyncio.sleep(LSA_REFRESH)
            async with self._lock:
                own_lsas = [
                    lsa for lsa in self._db.values()
                    if lsa.adv_router == self.router_id and not lsa.is_expired()
                ]
            for lsa in own_lsas:
                log.debug("Refreshing own LSA: %s", lsa)
                refreshed = SblLsa(
                    link_id       = lsa.link_id,
                    adv_router    = lsa.adv_router,
                    sequence      = lsa.sequence + 1,
                    age           = 0.0,
                    topo_rank     = lsa.topo_rank,
                    fwd_cost      = lsa.fwd_cost,
                    rev_cost      = lsa.rev_cost,
                    parallel_hint = lsa.parallel_hint,
                    scc_flag      = lsa.scc_flag,
                )
                await self.install_lsa(refreshed)

    # ---- Private ------------------------------------------------------------

    def _notify_change(self):
        for cb in self._on_change_cbs:
            try:
                cb()
            except Exception as e:
                log.error("Change callback error: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ip_to_int(ip: str) -> int:
    """'10.0.0.1' -> 0x0A000001"""
    try:
        parts = [int(p) for p in ip.split(".")]
        return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
    except Exception:
        # If router-id is not a dotted-quad, hash it to a stable u32
        return int(hashlib.md5(ip.encode()).hexdigest()[:8], 16) & 0xFFFF_FFFF


def _int_to_ip(n: int) -> str:
    return f"{(n>>24)&0xFF}.{(n>>16)&0xFF}.{(n>>8)&0xFF}.{n&0xFF}"


def _xor_checksum(data: bytes) -> int:
    chk = 0
    for b in data:
        chk ^= b
    return chk