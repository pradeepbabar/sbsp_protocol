# sbsp/daemon/main.py
import asyncio
import logging
from .hello import HelloProtocol
from .lsdb import LSDB
from .compute import ComputeEngine

logging.basicConfig(level=logging.INFO)

async def main():
    lsdb = LSDB()
    engine = ComputeEngine(lsdb)
    hello = HelloProtocol(lsdb, on_topo_change=engine.schedule_compute)

    await asyncio.gather(
        hello.run(),          # multicast Hello every 10s
        lsdb.flood_loop(),    # reliable LSA flooding
        engine.run(),         # BarrierSync + SSSP + FIB push
    )

asyncio.run(main())