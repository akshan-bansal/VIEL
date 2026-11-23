"""Directed, weighted venue network G_t = (V, E_t, W_t) (spec §7.1–7.2).

Edge i -> j: venue i leads venue j. Edges are added in sorted order and every
venue (including isolated ones) can be supplied as a node, so the graph — and
everything computed from it — does not depend on the order in which records
were loaded (spec §11.3 invariant 8). Invalid edges are rejected and reported,
not silently skipped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import networkx as nx

# Effective weights below this are numerically meaningless (and destabilise power iteration).
MIN_EFFECTIVE_WEIGHT = 1e-9


@dataclass
class GraphBuild:
    graph: nx.DiGraph
    rejected: list[dict] = field(default_factory=list)


def build_venue_graph(edge_records, venues=None, *, return_rejections: bool = False):
    """Build the venue graph. ``weight`` on each edge = weight x confidence."""
    graph = nx.DiGraph()
    for venue in sorted(venues or []):
        graph.add_node(venue)
    rejected: list[dict] = []
    seen: set[tuple[str, str]] = set()
    records = sorted(edge_records, key=lambda e: (str(e["source_venue"]), str(e["target_venue"])))
    for edge in records:
        source = edge["source_venue"]
        target = edge["target_venue"]
        try:
            weight = float(edge["weight"])
            confidence = float(edge["confidence"])
        except (TypeError, ValueError):
            rejected.append({**edge, "reason": "NON_NUMERIC"})
            continue
        if source == target:
            rejected.append({**edge, "reason": "SELF_LOOP"})
            continue
        if not math.isfinite(weight) or not math.isfinite(confidence):
            rejected.append({**edge, "reason": "NON_FINITE"})
            continue
        if weight <= 0 or not 0 <= confidence <= 1:
            rejected.append({**edge, "reason": "OUT_OF_RANGE"})
            continue
        if weight * confidence < MIN_EFFECTIVE_WEIGHT:
            rejected.append({**edge, "reason": "NEGLIGIBLE_WEIGHT"})
            continue
        if (source, target) in seen:
            raise ValueError(f"duplicate edge {source}->{target}: aggregate before building the graph")
        seen.add((source, target))
        graph.add_edge(source, target, weight=weight * confidence, raw_weight=weight, confidence=confidence)
    if return_rejections:
        return GraphBuild(graph, rejected)
    return graph
