"""
sbsp/algo/barrier_sssp.py
Sorting Barrier Directed Single-Source Shortest Path algorithm.

Phases:
  1. Topological sort via Kahn's algorithm  -> assign wave/rank to each node
  2. Detect SCCs (Tarjan) for cyclic subgraphs -> Bellman-Ford within each SCC
  3. Condense SCCs into super-nodes, build DAG
  4. Wave-by-wave relaxation with barrier sync between waves
  5. Return dist[] and prev[] maps
"""

from __future__ import annotations
import math
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Graph primitives
# ---------------------------------------------------------------------------

@dataclass
class Edge:
    src: str
    dst: str
    weight: float

    def __post_init__(self):
        if self.weight < 0:
            raise ValueError(f"Negative edge weight not supported: {self}")


@dataclass
class Graph:
    """Directed, weighted graph used for SBSP computation."""
    nodes: Set[str] = field(default_factory=set)
    adj: Dict[str, List[Tuple[str, float]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    radj: Dict[str, List[Tuple[str, float]]] = field(   # reverse adjacency
        default_factory=lambda: defaultdict(list)
    )

    def add_edge(self, src: str, dst: str, weight: float):
        self.nodes.update([src, dst])
        self.adj[src].append((dst, weight))
        self.radj[dst].append((src, weight))

    @classmethod
    def from_edge_list(cls, edges: List[Tuple[str, str, float]]) -> "Graph":
        g = cls()
        for u, v, w in edges:
            g.add_edge(u, v, w)
        return g


# ---------------------------------------------------------------------------
# Tarjan SCC
# ---------------------------------------------------------------------------

class TarjanSCC:
    """
    Kosaraju-Tarjan iterative SCC detection.
    Returns list of SCCs (each SCC is a frozenset of node IDs).
    SCCs of size 1 with no self-loop are plain DAG nodes.
    """

    def __init__(self, graph: Graph):
        self.graph = graph
        self._index_counter = [0]
        self._stack: List[str] = []
        self._lowlink: Dict[str, int] = {}
        self._index: Dict[str, int] = {}
        self._on_stack: Dict[str, bool] = {}
        self.sccs: List[frozenset] = []

    def run(self) -> List[frozenset]:
        for node in self.graph.nodes:
            if node not in self._index:
                self._strongconnect(node)
        return self.sccs

    def _strongconnect(self, start: str):
        # Iterative version to avoid Python recursion limit on large graphs
        call_stack = [(start, iter(self.graph.adj[start]))]
        self._index[start] = self._lowlink[start] = self._index_counter[0]
        self._index_counter[0] += 1
        self._stack.append(start)
        self._on_stack[start] = True

        while call_stack:
            node, neighbors = call_stack[-1]
            try:
                (nbr, _) = next(neighbors)
                if nbr not in self._index:
                    self._index[nbr] = self._lowlink[nbr] = self._index_counter[0]
                    self._index_counter[0] += 1
                    self._stack.append(nbr)
                    self._on_stack[nbr] = True
                    call_stack.append((nbr, iter(self.graph.adj[nbr])))
                elif self._on_stack.get(nbr):
                    self._lowlink[node] = min(
                        self._lowlink[node], self._index[nbr]
                    )
            except StopIteration:
                call_stack.pop()
                if call_stack:
                    parent, _ = call_stack[-1]
                    self._lowlink[parent] = min(
                        self._lowlink[parent], self._lowlink[node]
                    )
                # Root of an SCC
                if self._lowlink[node] == self._index[node]:
                    scc = []
                    while True:
                        w = self._stack.pop()
                        self._on_stack[w] = False
                        scc.append(w)
                        if w == node:
                            break
                    self.sccs.append(frozenset(scc))


# ---------------------------------------------------------------------------
# Bellman-Ford for intra-SCC relaxation
# ---------------------------------------------------------------------------

def bellman_ford_scc(
    nodes: frozenset,
    edges: List[Tuple[str, str, float]],
    dist: Dict[str, float],
    prev: Dict[str, Optional[str]],
    max_iter: Optional[int] = None,
) -> None:
    """
    Bounded Bellman-Ford relaxation within a single SCC.
    Modifies dist and prev in-place.
    max_iter defaults to |SCC nodes| - 1 (theoretical maximum path length).
    """
    scc_edges = [(u, v, w) for u, v, w in edges if u in nodes and v in nodes]
    iterations = max_iter if max_iter else max(len(nodes) - 1, 1)

    for _ in range(iterations):
        changed = False
        for u, v, w in scc_edges:
            if dist[u] < math.inf and dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                prev[v] = u
                changed = True
        if not changed:
            break   # early termination


# ---------------------------------------------------------------------------
# Main Sorting Barrier SSSP
# ---------------------------------------------------------------------------

def sorting_barrier_sssp(
    edges: List[Tuple[str, str, float]],
    source: str,
) -> Tuple[Dict[str, float], Dict[str, Optional[str]]]:
    """
    Sorting Barrier Directed SSSP.

    Args:
        edges:  List of (src, dst, weight) tuples. Weights must be >= 0.
        source: Source node ID (router ID string).

    Returns:
        dist:  {node -> shortest distance from source}
        prev:  {node -> predecessor node on shortest path}

    Algorithm:
        1. Build directed graph + detect SCCs via Tarjan
        2. Condense each SCC into a super-node
        3. Topological sort the condensed DAG (Kahn's)
        4. Assign wave rank to each super-node
        5. Wave-by-wave relaxation:
           - Within each wave: all nodes are independent, relax in parallel
           - At wave boundary: barrier sync (all wave-k distances settled)
           - For SCC super-nodes: run bounded Bellman-Ford internally
        6. Expand super-nodes back to real nodes
    """
    if not edges:
        return {source: 0.0}, {source: None}

    graph = Graph.from_edge_list(edges)

    if source not in graph.nodes:
        log.warning("Source %s not in graph nodes", source)
        graph.nodes.add(source)

    # ---- Step 1: Detect SCCs ------------------------------------------------
    scc_list = TarjanSCC(graph).run()

    # Map each node -> which SCC it belongs to (by index)
    node_to_scc: Dict[str, int] = {}
    for idx, scc in enumerate(scc_list):
        for n in scc:
            node_to_scc[n] = idx

    # ---- Step 2: Condensed DAG ----------------------------------------------
    # Edges between different SCCs only
    condensed_adj: Dict[int, List[Tuple[int, float]]] = defaultdict(list)
    condensed_in_degree: Dict[int, int] = defaultdict(int)
    condensed_nodes = set(range(len(scc_list)))
    seen_cond_edges: Set[Tuple[int, int]] = set()

    for u, v, w in edges:
        su, sv = node_to_scc[u], node_to_scc[v]
        if su != sv:
            key = (su, sv)
            if key not in seen_cond_edges:
                condensed_adj[su].append((sv, w))
                condensed_in_degree[sv] += 1
                seen_cond_edges.add(key)
            else:
                # Update to minimum weight for duplicate condensed edges
                for i, (dst, cw) in enumerate(condensed_adj[su]):
                    if dst == sv:
                        condensed_adj[su][i] = (sv, min(cw, w))
                        break

    for n in condensed_nodes:
        condensed_in_degree.setdefault(n, 0)

    # ---- Step 3: Topological sort of condensed DAG (Kahn's) ----------------
    waves: List[List[int]] = []
    queue = [n for n in condensed_nodes if condensed_in_degree[n] == 0]
    visited_cond: Set[int] = set(queue)

    while queue:
        waves.append(list(queue))
        next_wave = []
        for u in queue:
            for (v, _) in condensed_adj[u]:
                condensed_in_degree[v] -= 1
                if condensed_in_degree[v] == 0 and v not in visited_cond:
                    next_wave.append(v)
                    visited_cond.add(v)
        queue = next_wave

    # Any condensed nodes not visited = residual cycles (shouldn't happen after
    # proper SCC condensation, but defensive fallback)
    leftover = condensed_nodes - visited_cond
    if leftover:
        log.warning("Residual unvisited condensed nodes: %s", leftover)
        waves.append(list(leftover))

    # ---- Step 4: Initialize distances ---------------------------------------
    dist: Dict[str, float] = {n: math.inf for n in graph.nodes}
    prev: Dict[str, Optional[str]] = {n: None for n in graph.nodes}
    dist[source] = 0.0

    # For condensed SCC nodes: track minimum dist entering the SCC
    scc_dist: Dict[int, float] = {i: math.inf for i in range(len(scc_list))}
    source_scc = node_to_scc[source]
    scc_dist[source_scc] = 0.0

    # ---- Step 5: Wave-by-wave relaxation ------------------------------------
    for wave_idx, wave in enumerate(waves):
        log.debug("Wave %d: processing %d SCC super-nodes", wave_idx, len(wave))

        for scc_id in wave:
            scc_nodes = scc_list[scc_id]

            # Relax intra-SCC if it has cycles (size > 1 or self-loop)
            has_self_loop = any(
                (u, v, w) for u, v, w in edges
                if u in scc_nodes and v == u
            )
            if len(scc_nodes) > 1 or has_self_loop:
                bellman_ford_scc(scc_nodes, edges, dist, prev)

            # Relax outgoing edges from this SCC to next SCCs
            for u in scc_nodes:
                if dist[u] < math.inf:
                    for (v, w) in graph.adj[u]:
                        if node_to_scc[v] != scc_id:   # cross-SCC edge only
                            if dist[u] + w < dist[v]:
                                dist[v] = dist[u] + w
                                prev[v] = u

        # ========================= BARRIER ==================================
        # In a multi-threaded implementation, all threads sync here.
        # All wave-k distances are now final before wave k+1 starts.
        # In Python single-threaded mode this is implicit.
        # ====================================================================

    log.debug(
        "SSSP complete: %d reachable from %s",
        sum(1 for d in dist.values() if d < math.inf),
        source,
    )
    return dist, prev


# ---------------------------------------------------------------------------
# Path reconstruction
# ---------------------------------------------------------------------------

def reconstruct_path(
    prev: Dict[str, Optional[str]],
    destination: str,
) -> Optional[List[str]]:
    """Trace back from destination to source via prev[] map."""
    path = []
    current: Optional[str] = destination
    visited: Set[str] = set()
    while current is not None:
        if current in visited:
            log.error("Cycle detected in prev[] map at %s", current)
            return None
        visited.add(current)
        path.append(current)
        current = prev.get(current)
    path.reverse()
    return path if len(path) > 1 or path[0] == destination else None


def next_hop(
    prev: Dict[str, Optional[str]],
    source: str,
    destination: str,
) -> Optional[str]:
    """Return the immediate next hop from source toward destination."""
    path = reconstruct_path(prev, destination)
    if path is None or len(path) < 2:
        return None
    # path[0] == source
    return path[1] if path[0] == source else None