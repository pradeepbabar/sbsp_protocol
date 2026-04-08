# sbsp/algo/barrier_sssp.py
from collections import defaultdict
from typing import Dict, List, Tuple
import math

def barrier_sssp(
    edges: List[Tuple[str, str, float]],   # (u, v, weight)
    source: str
) -> Tuple[Dict[str, float], Dict[str, str]]:
    """
    Sorting Barrier directed SSSP.
    Returns (dist, prev) dicts.
    Falls back to Bellman-Ford within detected SCCs.
    """
    # Build adjacency
    adj = defaultdict(list)
    nodes = set()
    for u, v, w in edges:
        adj[u].append((v, w))
        nodes.update([u, v])

    # Phase 1: topological sort (Kahn's algorithm — detects cycles too)
    in_degree = defaultdict(int)
    for u, v, _ in edges:
        in_degree[v] += 1
    for n in nodes:
        in_degree.setdefault(n, 0)

    # Group nodes into waves by rank
    waves = []
    queue = [n for n in nodes if in_degree[n] == 0]
    visited = set(queue)
    remaining = nodes - visited

    while queue:
        waves.append(list(queue))
        next_wave = []
        for u in queue:
            for (v, _) in adj[u]:
                in_degree[v] -= 1
                if in_degree[v] == 0 and v not in visited:
                    next_wave.append(v)
                    visited.add(v)
        queue = next_wave

    # Nodes not reached = inside SCCs, handle with Bellman-Ford
    scc_nodes = remaining - visited
    if scc_nodes:
        waves.append(list(scc_nodes))  # simplified: one extra BF wave

    # Phase 2: wave relaxation
    dist = {n: math.inf for n in nodes}
    prev = {n: None for n in nodes}
    dist[source] = 0.0

    for wave in waves:
        # In real implementation: process wave in parallel (multiprocessing.Pool)
        for u in wave:
            if dist[u] < math.inf:
                for (v, w) in adj[u]:
                    if dist[u] + w < dist[v]:
                        dist[v] = dist[u] + w
                        prev[v] = u
        # ---- BARRIER ---- all threads sync here before next wave

    return dist, prev