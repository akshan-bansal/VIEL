"""Venue influence from the directed lead-lag graph (spec §7.2).

Orientation: edge i -> j means i leads j, so influence is about *outgoing*
reach. NetworkX's eigenvector centrality and PageRank reward *incoming* edges;
they are therefore computed on the reversed graph.

Primary measure — Katz out-reach (baseline-free):

    x = sum_{k>=1} alpha^(k-1) W^k 1 = (I - alpha W)^-1 W 1

It counts direct and indirect (discounted) downstream influence, is zero for a
venue that leads nobody, is well defined on DAGs (where eigenvector centrality
degenerates) and on cyclic graphs (alpha * rho(W) < 1 is enforced). Scores are
normalised so the most influential venue = 1.

Alternatives (out-strength, reversed PageRank, reversed eigenvector centrality
when the graph is strongly connected) are reported with their Kendall rank
agreement against the primary measure. High centrality is structural
information, not evidence of toxic flow (spec §7.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx
import numpy as np
from scipy import stats


@dataclass
class InfluenceResult:
    method: str
    scores: dict[str, float]
    raw_scores: dict[str, float]
    alternatives: dict[str, dict[str, float] | None]
    rank_agreement: dict[str, float | None]
    strongly_connected_components: list[list[str]]
    is_strongly_connected: bool
    alpha: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "orientation": "edge i -> j means i leads j; influence = outgoing reach",
            "scores": {k: round(v, 8) for k, v in sorted(self.scores.items())},
            "raw_scores": {k: round(v, 8) for k, v in sorted(self.raw_scores.items())},
            "alternatives": {
                name: ({k: round(v, 8) for k, v in sorted(vals.items())} if vals is not None else None)
                for name, vals in sorted(self.alternatives.items())
            },
            "rank_agreement_kendall_tau": {k: (round(v, 6) if v is not None else None)
                                           for k, v in sorted(self.rank_agreement.items())},
            "strongly_connected_components": self.strongly_connected_components,
            "is_strongly_connected": self.is_strongly_connected,
            "katz_alpha": self.alpha,
            "notes": self.notes,
        }


def _normalise(values: dict[str, float]) -> dict[str, float]:
    peak = max(values.values(), default=0.0)
    if peak <= 0:
        return {k: 0.0 for k in values}
    return {k: v / peak for k, v in values.items()}


def katz_out_reach(graph: nx.DiGraph) -> tuple[dict[str, float], float]:
    nodes = sorted(graph.nodes)
    if not nodes:
        return {}, 0.0
    W = nx.to_numpy_array(graph, nodelist=nodes, weight="weight")
    rho = float(max(abs(np.linalg.eigvals(W)))) if W.any() else 0.0
    alpha = 0.5 if rho <= 1.0 else 0.5 / rho
    ones = np.ones(len(nodes))
    x = np.linalg.solve(np.eye(len(nodes)) - alpha * W, W @ ones)
    x = np.where(np.abs(x) < 1e-15, 0.0, x)
    return {n: float(v) for n, v in zip(nodes, x, strict=True)}, alpha


def _kendall(a: dict[str, float], b: dict[str, float] | None) -> float | None:
    if b is None:
        return None
    keys = sorted(a)
    xa = [a[k] for k in keys]
    xb = [b[k] for k in keys]
    if len(set(xa)) < 2 or len(set(xb)) < 2:
        return None
    tau = stats.kendalltau(xa, xb).statistic
    return None if tau is None or np.isnan(tau) else float(tau)


def calculate_influence(graph: nx.DiGraph) -> InfluenceResult:
    nodes = sorted(graph.nodes)
    notes: list[str] = []
    raw, alpha = katz_out_reach(graph)
    scores = _normalise(raw)

    out_strength = {n: float(sum(d["weight"] for _, _, d in graph.out_edges(n, data=True))) for n in nodes}
    reversed_graph = graph.reverse(copy=True)
    pagerank = None
    if graph.number_of_edges() > 0:
        try:
            pagerank = nx.pagerank(reversed_graph, alpha=0.85, weight="weight", max_iter=1000,
                                   nstart={n: 1 / len(nodes) for n in nodes})
        except nx.PowerIterationFailedConvergence:
            notes.append("reversed PageRank did not converge; alternative omitted")
    else:
        notes.append("graph has no edges: every venue has zero influence")

    sccs = sorted((sorted(c) for c in nx.strongly_connected_components(graph)), key=lambda c: (-len(c), c))
    strongly = len(nodes) > 0 and len(sccs) == 1
    eigen = None
    if strongly and graph.number_of_edges() > 0:
        try:
            eigen = nx.eigenvector_centrality_numpy(reversed_graph, weight="weight")
            eigen = {k: abs(float(v)) for k, v in eigen.items()}
        except (nx.NetworkXException, ArithmeticError, ValueError) as exc:  # pragma: no cover
            notes.append(f"eigenvector centrality did not converge: {exc}")
    else:
        notes.append("eigenvector centrality not applicable: graph is not strongly connected "
                     f"({len(sccs)} components)")

    alternatives = {
        "out_strength": _normalise(out_strength),
        "pagerank_reversed": _normalise(pagerank) if pagerank else None,
        "eigenvector_reversed": _normalise(eigen) if eigen else None,
    }
    agreement = {name: _kendall(scores, vals) for name, vals in alternatives.items()}
    return InfluenceResult(
        method="katz_out_reach",
        scores=scores,
        raw_scores=raw,
        alternatives=alternatives,
        rank_agreement=agreement,
        strongly_connected_components=sccs,
        is_strongly_connected=strongly,
        alpha=alpha,
        notes=notes,
    )


def price_discovery_share(graph: nx.DiGraph) -> dict[str, float]:
    total = sum(d["weight"] for _, _, d in graph.edges(data=True))
    out = {n: 0.0 for n in sorted(graph.nodes)}
    if total <= 0:
        return out
    for n in out:
        out[n] = sum(d["weight"] for _, _, d in graph.out_edges(n, data=True)) / total
    return out
