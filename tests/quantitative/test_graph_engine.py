"""Spec §7.3 graph validation and §12.3 graph KPIs."""

import numpy as np
import pytest

from venue_integrity.graph.centrality import calculate_influence, katz_out_reach, price_discovery_share
from venue_integrity.graph.lead_lag import session_returns, shift_within_sessions
from venue_integrity.graph.network_builder import build_venue_graph
from venue_integrity.graph.regime_detection import classify_sessions
from venue_integrity.graph.validation import run_graph_benchmarks
from venue_integrity.schemas.enums import MarketRegime

BENCHMARKS = run_graph_benchmarks()


@pytest.mark.parametrize("result", BENCHMARKS, ids=[b.case for b in BENCHMARKS])
def test_benchmark(result):
    assert result.passed, result.detail


@pytest.mark.parametrize("seed", [11, 23, 99])
def test_benchmarks_robust_to_seed(seed):
    failed = [b.case for b in run_graph_benchmarks(seed) if not b.passed]
    assert not failed


def test_graph_builder_rejects_invalid_edges():
    build = build_venue_graph([
        {"source_venue": "A", "target_venue": "A", "weight": 1, "confidence": 1},
        {"source_venue": "A", "target_venue": "B", "weight": float("nan"), "confidence": 1},
        {"source_venue": "A", "target_venue": "B", "weight": -1, "confidence": 1},
        {"source_venue": "A", "target_venue": "B", "weight": 1, "confidence": 1.5},
        {"source_venue": "A", "target_venue": "B", "weight": "x", "confidence": 1},
        {"source_venue": "A", "target_venue": "C", "weight": 0.5, "confidence": 0.8},
    ], venues=["A", "B", "C", "D"], return_rejections=True)
    assert sorted(r["reason"] for r in build.rejected) == ["NON_FINITE", "NON_NUMERIC", "OUT_OF_RANGE", "OUT_OF_RANGE", "SELF_LOOP"]
    assert build.graph["A"]["C"]["weight"] == pytest.approx(0.4)
    assert set(build.graph.nodes) == {"A", "B", "C", "D"}  # isolated venues kept
    with pytest.raises(ValueError, match="duplicate edge"):
        build_venue_graph([{"source_venue": "A", "target_venue": "B", "weight": 1, "confidence": 1}] * 2)


def test_katz_out_reach_orientation_and_indirect_influence():
    g = build_venue_graph([{"source_venue": "C", "target_venue": "A", "weight": 1, "confidence": 1},
                           {"source_venue": "A", "target_venue": "B", "weight": 1, "confidence": 1}], venues=["D"])
    raw, alpha = katz_out_reach(g)
    assert raw["C"] > raw["A"] > raw["B"] == 0 and raw["D"] == 0  # C reaches B through A
    inf = calculate_influence(g)
    assert inf.scores["C"] == 1.0 and inf.alternatives["eigenvector_reversed"] is None
    assert not inf.is_strongly_connected
    assert price_discovery_share(g) == {"A": 0.5, "B": 0.0, "C": 0.5, "D": 0.0}


def test_cyclic_graph_uses_convergent_alpha():
    edges = [{"source_venue": s, "target_venue": t, "weight": 1.0, "confidence": 1.0}
             for s, t in [("A", "B"), ("B", "C"), ("C", "A"), ("A", "C")]]
    inf = calculate_influence(build_venue_graph(edges))
    assert inf.is_strongly_connected and inf.alternatives["eigenvector_reversed"] is not None
    assert inf.alpha * max(abs(np.linalg.eigvals(np.array([[0, 1, 1], [0, 0, 1], [1, 0, 0]])))) < 1
    assert inf.scores["A"] == 1.0


def test_empty_graph_has_zero_influence():
    inf = calculate_influence(build_venue_graph([], venues=["A", "B"]))
    assert inf.scores == {"A": 0.0, "B": 0.0}


def test_high_centrality_is_not_a_risk_penalty():
    from venue_integrity.analytics.venue_risk import venue_risk_score

    weights = {"adverse_selection": 0.4, "market_impact": 0.3, "latency": 0.15, "data_quality": 0.15}
    comps = {"adverse_selection": 0.2, "market_impact": 0.1, "latency": 0.1, "data_quality": 0.0}
    assert venue_risk_score(comps, weights) == pytest.approx(0.4 * 0.2 + 0.3 * 0.1 + 0.15 * 0.1)
    assert venue_risk_score({**comps, "latency": None}, weights) is None  # never imputes zero


def test_session_boundaries_respected():
    sess = np.array([0, 0, 0, 1, 1, 1])
    x = np.arange(6, dtype=float)
    assert np.isnan(shift_within_sessions(x, 1, sess)[3])
    r = session_returns(np.exp(x), sess)
    assert np.isnan(r[0]) and np.isnan(r[3]) and r[4] == pytest.approx(1.0)


def test_regime_labels():
    labels = classify_sessions({0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0, 4: float("nan")}, high_percentile=75, low_percentile=25)
    assert labels[0] is MarketRegime.LOW_VOLATILITY and labels[3] is MarketRegime.HIGH_VOLATILITY
    assert labels[4] is MarketRegime.UNKNOWN


def test_scenario_graph_recovers_ground_truth(scenario_run, scenario):
    graph = scenario_run["graph"]
    lags = {v.venue_id: v.quote_lag_ticks for v in scenario.config.venues}
    assert graph.influence.scores["VENUE_C"] == 1.0
    assert all(lags[e.source_venue] < lags[e.target_venue] for e in graph.lead_lag.edges)
    regimes = {k: v.value for k, v in graph.regimes.items()}
    assert regimes == scenario.ground_truth["session_regimes"]
