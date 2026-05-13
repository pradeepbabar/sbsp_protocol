"""
sbsp/daemon/advertise.py
Subnet Advertisement for SBSP.

Each router originates a special Prefix-LSA (type 0x09) for its own
loopback subnet (e.g. 192.168.1.0/24). This LSA is flooded to all
neighbours so every router in the area learns the prefix and installs
a route toward it via the SBSP-computed shortest path.

Prefix-LSA wire format (36 bytes, big-endian):
  Offset  Len  Field
  ------  ---  -----
   0       1   LSA type     (0x09 = Prefix)
   1       1   Flags        (bit0=withdraw)
   2       2   Length       (u16 = 36)
   4       4   Adv router   (u32, router-id)
   8       4   LS sequence  (u32)
  12       2   LS age       (u16, seconds)
  14       2   Reserved
  16       4   Prefix       (u32, network address e.g. 192.168.1.0)
  20       1   Prefix len   (u8, e.g. 24)
  21       3   Reserved
  24       4   Metric       (u32, cost to reach this prefix from adv_router)
  28       4   Next-hop     (u32, next-hop IP for this prefix, 0=self)
  32       4   Checksum     (u32, sum of bytes 0-31)
  Total: 36 bytes
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

LSA_TYPE_PREFIX   = 0x09
PREFIX_LSA_SIZE   = 36
FLAG_WITHDRAW     = 0x01
PREFIX_MAX_AGE    = 3600
PREFIX_REFRESH    = 1800


# ---------------------------------------------------------------------------
# Prefix LSA
# ---------------------------------------------------------------------------

@dataclass
class PrefixLsa:
    """One subnet advertisement — a router telling the area about its prefix."""
    adv_router:  str            # router-id of the originator
    sequence:    int
    age:         float
    prefix:      str            # e.g. "192.168.1.0"
    prefix_len:  int            # e.g. 24
    metric:      int            # cost at the originating router (usually 0)
    next_hop:    str = "0.0.0.0"  # 0.0.0.0 = reachable via adv_router itself
    withdraw:    bool = False
    received_at: float = field(default_factory=time.monotonic)

    def key(self) -> Tuple[str, str]:
        """Unique: (prefix/len, adv_router)."""
        return (f"{self.prefix}/{self.prefix_len}", self.adv_router)

    def current_age(self) -> float:
        return self.age + (time.monotonic() - self.received_at)

    def is_expired(self) -> bool:
        return self.current_age() >= PREFIX_MAX_AGE

    def network(self) -> str:
        return f"{self.prefix}/{self.prefix_len}"

    def encode(self) -> bytes:
        flags   = FLAG_WITHDRAW if self.withdraw else 0
        adv_int = _ip_to_int(self.adv_router)
        pfx_int = _ip_to_int(self.prefix)
        nh_int  = _ip_to_int(self.next_hop)
        age_int = min(int(self.current_age()), 65535)

        body = struct.pack(
            "!BBH II HH I B3x I I",
            LSA_TYPE_PREFIX,          # 1  B
            flags,                    # 1  B
            PREFIX_LSA_SIZE,          # 2  H
            adv_int,                  # 4  I  adv_router
            self.sequence & 0xFFFFFFFF,  # 4  I  sequence
            age_int,                  # 2  H
            0,                        # 2  H  reserved
            pfx_int,                  # 4  I  prefix
            self.prefix_len & 0xFF,   # 1  B
                                      # 3x pad
            self.metric & 0xFFFFFFFF, # 4  I
            nh_int,                   # 4  I  next-hop
        )                             # = 32 bytes
        chksum = sum(body) & 0xFFFFFFFF
        return body + struct.pack("!I", chksum)

    @classmethod
    def decode(cls, data: bytes) -> Optional["PrefixLsa"]:
        if len(data) < PREFIX_LSA_SIZE:
            return None
        try:
            (lsa_type, flags, length,
             adv_int, sequence,
             age, _reserved,
             pfx_int, pfx_len,
             metric, nh_int,
             chksum) = struct.unpack("!BBH II HH I B3x I I I", data[:PREFIX_LSA_SIZE])
        except struct.error as e:
            log.error("PrefixLSA decode error: %s", e)
            return None

        if lsa_type != LSA_TYPE_PREFIX:
            return None

        expected = sum(data[:32]) & 0xFFFFFFFF
        if expected != chksum:
            log.warning("PrefixLSA checksum mismatch")
            return None

        return cls(
            adv_router  = _int_to_ip(adv_int),
            sequence    = sequence,
            age         = float(age),
            prefix      = _int_to_ip(pfx_int),
            prefix_len  = pfx_len,
            metric      = metric,
            next_hop    = _int_to_ip(nh_int),
            withdraw    = bool(flags & FLAG_WITHDRAW),
        )

    def __repr__(self):
        return (f"PrefixLsa({self.adv_router} adv {self.prefix}/{self.prefix_len} "
                f"metric={self.metric} seq={self.sequence})")


# ---------------------------------------------------------------------------
# Prefix LSDB
# ---------------------------------------------------------------------------

class PrefixLSDB:
    """
    Stores Prefix-LSAs separately from SBL-LSAs.
    Provides the prefix table used by ComputeEngine to install /N routes.
    """

    def __init__(self, router_id: str):
        self.router_id = router_id
        self._db: Dict[Tuple[str, str], PrefixLsa] = {}
        self._lock = asyncio.Lock()
        self._on_change_cbs = []
        self._flood_queue: asyncio.Queue = asyncio.Queue()

    def register_change_callback(self, cb):
        self._on_change_cbs.append(cb)

    async def install(self, lsa: PrefixLsa, from_neighbour: str = "") -> bool:
        async with self._lock:
            key = lsa.key()
            existing = self._db.get(key)
            if existing:
                if lsa.sequence <= existing.sequence:
                    return False
            self._db[key] = lsa
            log.info("PrefixLSDB: %s", lsa)
            await self._flood_queue.put((lsa, from_neighbour))

        self._notify()
        return True

    async def originate(
        self,
        prefix:     str,
        prefix_len: int,
        metric:     int = 0,
    ) -> PrefixLsa:
        """Create and install a self-originated prefix LSA."""
        key = (f"{prefix}/{prefix_len}", self.router_id)
        async with self._lock:
            existing = self._db.get(key)
            seq = (existing.sequence + 1) if existing else 1

        lsa = PrefixLsa(
            adv_router = self.router_id,
            sequence   = seq,
            age        = 0.0,
            prefix     = prefix,
            prefix_len = prefix_len,
            metric     = metric,
        )
        await self.install(lsa)
        log.info("Originated prefix LSA: %s/%d metric=%d", prefix, prefix_len, metric)
        return lsa

    async def withdraw(self, prefix: str, prefix_len: int):
        key = (f"{prefix}/{prefix_len}", self.router_id)
        async with self._lock:
            lsa = self._db.get(key)
            if lsa:
                lsa.withdraw = True
                lsa.age = PREFIX_MAX_AGE
                await self._flood_queue.put((lsa, ""))
        self._notify()

    def get_prefix_table(self) -> List[PrefixLsa]:
        """All active (non-expired, non-withdrawn) prefix LSAs."""
        return [
            lsa for lsa in self._db.values()
            if not lsa.is_expired() and not lsa.withdraw
        ]

    def summary(self) -> str:
        lines = [f"Prefix LSDB ({self.router_id}):"]
        for lsa in sorted(self._db.values(), key=lambda l: l.prefix):
            lines.append(f"  {lsa}")
        return "\n".join(lines)

    async def aging_loop(self):
        while True:
            await asyncio.sleep(30)
            async with self._lock:
                expired = [k for k, v in self._db.items() if v.is_expired()]
                for k in expired:
                    log.info("Aged out prefix LSA: %s", k)
                    del self._db[k]
            if expired:
                self._notify()

    async def flood_loop(self, send_func=None):
        while True:
            lsa, from_nbr = await self._flood_queue.get()
            encoded = lsa.encode()
            if send_func:
                try:
                    await send_func(encoded, exclude=from_nbr)
                except Exception as e:
                    log.error("Prefix flood error: %s", e)

    async def refresh_loop(self):
        while True:
            await asyncio.sleep(PREFIX_REFRESH)
            async with self._lock:
                own = [lsa for lsa in self._db.values()
                       if lsa.adv_router == self.router_id and not lsa.is_expired()]
            for lsa in own:
                refreshed = PrefixLsa(
                    adv_router = lsa.adv_router,
                    sequence   = lsa.sequence + 1,
                    age        = 0.0,
                    prefix     = lsa.prefix,
                    prefix_len = lsa.prefix_len,
                    metric     = lsa.metric,
                )
                await self.install(refreshed)

    def _notify(self):
        for cb in self._on_change_cbs:
            try:
                cb()
            except Exception as e:
                log.error("PrefixLSDB change callback error: %s", e)


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