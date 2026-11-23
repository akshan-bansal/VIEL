"""Synthetic benchmark suite for the graph engine (spec §7.3, §12.3).

Each case builds venue midpoints with a known structure and checks that the
lead-lag estimator and influence measure recover it. The same suite runs in
the test-suite and inside every audit run, so the evidence package records
that the deployed graph engine passed its benchmarks.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

import numpy as np

from venue_integrity.graph.centrality import calculate_influence
from venue_integrity.graph.lead_lag import estimate_lead_lag, shift_within_sessions
from venue_integrity.graph.network_builder import build_venue_graph


@dataclass(frozen=True)
class BenchmarkResult:
    case: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"case": self.case, "passed": self.passed, "detail": self.detail}


def synthetic_mids(
    lags: dict[str, int],
    *,
    ticks: int = 30_000,
    sessions: int = 10,
    vol_bps: float = 1.0,
    noise_bps: float = 0.2,
    seed: int = 7,
    factor_ar: float = 0.0,
    driver: np.ndarray | None = None,
) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray]:
    """Venue v's midpoint = common efficient price lagged by ``lags[v]`` ticks plus noise.

    Returns (mids, session labels, efficient-price returns)."""
    rng = np.random.default_rng(seed)
    sess = np.repeat(np.arange(sessions), ticks // sessions)
    if driver is None:
        shocks = rng.normal(0, vol_bps / 1e4, len(sess))
        if factor_ar:
            for t in range(1, len(shocks)):
                shocks[t] += factor_ar * shocks[t - 1]
    else:
        shocks = driver
    log_p = np.log(100.0) + np.cumsum(shocks)
    mids = {}
    for venue, lag in sorted(lags.items()):
        lagged = shift_within_sessions(log_p, lag, sess) if lag else log_p.copy()
        lagged = np.where(np.isnan(lagged), log_p, lagged)
        mids[venue] = np.exp(lagged + rng.normal(0, noise_bps / 1e4, len(sess)))
    return mids, sess, shocks


def _influence(mids, sess, **kw):
    ll = estimate_lead_lag(mids, sess, max_lag=3, significance=0.01, min_observations=500, **kw)
    graph = build_venue_graph(ll.edge_records(), ll.venues)
    return ll, calculate_influence(graph)


def _top(scores: dict[str, float]) -> str | None:
    if not scores or max(scores.values()) <= 0:
        return None
    return max(sorted(scores), key=lambda k: scores[k])


def run_graph_benchmarks(seed: int = 7) -> list[BenchmarkResult]:
    results: list[BenchmarkResult] = []

    # 1. known dominant node
    mids, sess, _ = synthetic_mids({"A": 1, "B": 2, "C": 0, "D": 1}, seed=seed)
    ll, inf = _influence(mids, sess)
    edges = {(e.source_venue, e.target_venue) for e in ll.edges}
    ok = _top(inf.scores) == "C" and all(s == "C" or (s, t) in {("A", "B"), ("D", "B")} for s, t in edges)
    results.append(BenchmarkResult("known_dominant_node", ok, f"top={_top(inf.scores)} edges={sorted(edges)}"))

    # 2a. reversed direction of the true relationship moves the dominant node
    mids2, sess2, _ = synthetic_mids({"A": 0, "B": 2, "C": 1, "D": 1}, seed=seed + 1)
    _, inf2 = _influence(mids2, sess2)
    results.append(BenchmarkResult("randomized_edge_direction_reversal", _top(inf2.scores) == "A",
                                   f"leader moved to A: top={_top(inf2.scores)}"))
    # 2b. destroying temporal order (permuting the leader's returns) removes its edges
    rng = np.random.default_rng(seed)
    perm = {k: v.copy() for k, v in mids.items()}
    r = np.diff(np.log(perm["C"]))
    perm["C"] = np.exp(np.concatenate([[np.log(perm["C"][0])], np.log(perm["C"][0]) + np.cumsum(rng.permutation(r))]))
    llp, _ = _influence(perm, sess)
    c_edges = [e for e in llp.edges if e.source_venue == "C"]
    results.append(BenchmarkResult("permuted_leader_has_no_edges", not c_edges, f"C edges after permutation: {len(c_edges)}"))

    # 3. timestamp shift: delaying C's timestamps by 3 ticks turns it into a follower
    shifted = dict(mids)
    shifted["C"] = shift_within_sessions(mids["C"], 3, sess)
    _, inf3 = _influence(shifted, sess)
    ok3 = inf3.scores.get("C", 0) < 0.5 and _top(inf3.scores) != "C"
    results.append(BenchmarkResult("timestamp_shifted_leader", ok3, f"C influence after +3 tick shift = {inf3.scores.get('C', 0):.3f}"))

    # 4a. contemporaneous, autocorrelated common factor: no edges
    mids4, sess4, _ = synthetic_mids({"A": 0, "B": 0, "C": 0, "D": 0}, seed=seed + 2, factor_ar=0.3)
    ll4, _ = _influence(mids4, sess4)
    results.append(BenchmarkResult("common_factor_no_edges", not ll4.edges, f"edges={len(ll4.edges)}"))
    # 4b. exogenous driver with venue-specific lags: control removes spurious venue edge
    rng = np.random.default_rng(seed + 3)
    sess5 = np.repeat(np.arange(10), 3000)
    factor = rng.normal(0, 1e-4, len(sess5))
    lp = np.log(100) + np.cumsum(factor)
    a = np.exp(np.nan_to_num(shift_within_sessions(lp, 1, sess5), nan=lp[0]) + rng.normal(0, 2e-5, len(sess5)))
    b = np.exp(np.nan_to_num(shift_within_sessions(lp, 2, sess5), nan=lp[0]) + rng.normal(0, 2e-5, len(sess5)))
    without = estimate_lead_lag({"A": a, "B": b}, sess5, max_lag=3)
    with_ctrl = estimate_lead_lag({"A": a, "B": b}, sess5, max_lag=3, common_factor_returns=factor)
    ok4b = any(e.source_venue == "A" for e in without.edges) and not with_ctrl.edges
    results.append(BenchmarkResult("exogenous_factor_control", ok4b,
                                   f"edges without control={len(without.edges)}, with control={len(with_ctrl.edges)}"))

    # 5. missing observations
    holey = {k: v.copy() for k, v in mids.items()}
    rng = np.random.default_rng(seed + 4)
    holey["A"][rng.random(len(sess)) < 0.10] = np.nan
    holey["D"][rng.random(len(sess)) < 0.995] = np.nan
    ll5, inf5 = _influence(holey, sess)
    ok5 = _top(inf5.scores) == "C" and "D" in ll5.excluded_venues
    results.append(BenchmarkResult("missing_observations", ok5,
                                   f"top={_top(inf5.scores)}, excluded={sorted(ll5.excluded_venues)}"))

    # 6. disconnected clusters
    m1, s1, _ = synthetic_mids({"C": 0, "A": 1}, seed=seed + 5)
    m2, _, _ = synthetic_mids({"D": 0, "B": 1}, seed=seed + 6)
    ll6, inf6 = _influence({**m1, **m2}, s1)
    e6 = {(e.source_venue, e.target_venue) for e in ll6.edges}
    ok6 = e6 == {("C", "A"), ("D", "B")} and inf6.scores["C"] > 0 and inf6.scores["D"] > 0 and not inf6.is_strongly_connected
    results.append(BenchmarkResult("disconnected_clusters", ok6, f"edges={sorted(e6)}"))

    # 7. regime change: leadership switches half-way
    first, s_a, _ = synthetic_mids({"C": 0, "A": 1, "B": 1}, seed=seed + 7, sessions=10)
    second, _, _ = synthetic_mids({"C": 1, "A": 0, "B": 1}, seed=seed + 8, sessions=10)
    half = len(s_a) // 2
    mixed = {k: np.concatenate([first[k][:half], second[k][half:] * first[k][half - 1] / second[k][half]]) for k in first}
    _, inf_a = _influence({k: v[:half] for k, v in mixed.items()}, s_a[:half])
    _, inf_b = _influence({k: v[half:] for k, v in mixed.items()}, s_a[half:])
    ok7 = _top(inf_a.scores) == "C" and _top(inf_b.scores) == "A"
    results.append(BenchmarkResult("changing_regimes", ok7, f"first={_top(inf_a.scores)}, second={_top(inf_b.scores)}"))

    # 8. feed-latency confounding
    flat, s8, _ = synthetic_mids({"A": 0, "B": 0, "C": 0}, seed=seed + 9)
    delayed = dict(flat)
    delayed["B"] = shift_within_sessions(flat["B"], 2, s8)
    naive = estimate_lead_lag(delayed, s8, max_lag=3)
    adjusted = estimate_lead_lag(delayed, s8, max_lag=3, latency_adjustment_ticks={"B": 2})
    ok8 = any(e.target_venue == "B" for e in naive.edges) and not adjusted.edges
    results.append(BenchmarkResult("feed_latency_confounding", ok8,
                                   f"naive edges into B={sum(e.target_venue == 'B' for e in naive.edges)}, "
                                   f"after adjustment={len(adjusted.edges)}"))

    # 9. determinism: record order does not matter
    records = ll.edge_records()
    shuffled = records[:]
    random.Random(seed).shuffle(shuffled)
    g1 = calculate_influence(build_venue_graph(records, ll.venues)).scores
    g2 = calculate_influence(build_venue_graph(shuffled, list(reversed(ll.venues)))).scores
    results.append(BenchmarkResult("load_order_invariance", g1 == g2, "identical scores for shuffled input"))
    return results
