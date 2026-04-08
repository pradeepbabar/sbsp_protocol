# tests/test_algo.py
import pytest
from sbsp.algo.barrier_sssp import barrier_sssp

def test_simple_dag():
    edges = [
        ("R1","R2",5), ("R1","R3",3),
        ("R3","R2",1), ("R2","R4",2), ("R3","R4",6)
    ]
    dist, prev = barrier_sssp(edges, "R1")
    assert dist["R2"] == 4   # R1->R3->R2
    assert dist["R4"] == 6   # R1->R3->R2->R4

def test_asymmetric():
    edges = [("A","B",1), ("B","A",10)]  # directed, asymmetric
    dist, _ = barrier_sssp(edges, "A")
    assert dist["B"] == 1
    dist2, _ = barrier_sssp(edges, "B")
    assert dist2["A"] == 10  # B->A costs 10, not 1