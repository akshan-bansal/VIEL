"""Spec §8, §9, §10: TCA attribution on the scenario, venue intelligence, SOR audit."""

import pytest

from venue_integrity.router.decision_reconstruction import ReconstructionStatus
from venue_integrity.simulation.outcome_model import SimulatedOutcomeModel


def test_tca_attribution_and_coverage(scenario_run):
    tca, report = scenario_run["tca"], scenario_run["report"]
    assert tca.coverage["coverage_ratio"] >= 0.999
    assert tca.coverage["stale_midpoints_included"] == 0
    assert "UNRESOLVED" in tca.venues  # unresolved fills kept in their own bucket
    assert tca.venues["UNRESOLVED"].fee_adjusted_cost_bps is None  # unknown fee is not zero
    attributed = sum(v.quantity for k, v in tca.venues.items())
    active = sum(float(e.quantity) for e in report.executions if e.status.value == "ACTIVE")
    assert attributed == pytest.approx(active)  # nothing double counted, nothing lost
    for venue in tca.venues.values():
        for h in venue.horizons.values():
            assert h["complete"] + h["incomplete"] + h["stale"] == venue.fills


def test_measured_adverse_selection_ranks_leader_highest(scenario_run):
    tca = scenario_run["tca"]
    as_1s = {v: tca.venues[v].as_bps(1000) for v in ("VENUE_A", "VENUE_B", "VENUE_C")}
    assert max(as_1s, key=as_1s.get) == "VENUE_C"


def test_venue_intelligence_contract(scenario_run, scenario):
    p = scenario_run["pipeline"]
    intel, _ = p.venue_intelligence(scenario_run["tca"], scenario_run["graph"], scenario.market_data,
                                    scenario_run["report"], calculated_at=scenario.config.first_session)
    by_id = {v.venue_id: v for v in intel}
    assert by_id["VENUE_C"].graph_influence == 1.0
    assert by_id["VENUE_X"].observations == 0 and by_id["VENUE_X"].venue_risk is None
    # under registry v2.0 VENUE_C's attributed fills often lack a resolved intended venue
    assert by_id["VENUE_C"].data_quality_score < by_id["VENUE_A"].data_quality_score
    assert all(v.model_version == "1.0" for v in intel)


def test_decision_reconstruction_finds_planted_defects(scenario_run, scenario):
    p = scenario_run["pipeline"]
    results = p.reconstruct_decisions(scenario.decisions, scenario_run["report"], scenario_run["tca"])
    bad = {r.decision_id: r.status for r in results if r.status is not ReconstructionStatus.REPRODUCED}
    planted = scenario.ground_truth["planted_faults"]
    assert set(bad) == set(planted["lookahead_decisions"]) | set(planted["stale_weight_decisions"])
    assert bad[planted["lookahead_decisions"][0]] is ReconstructionStatus.NOT_REPRODUCIBLE
    linked = [r for r in results if r.outcome is not None]
    assert len(linked) == len(results)


def test_feature_integrity_flags_contaminated_venue(scenario_run, scenario):
    p = scenario_run["pipeline"]
    summary = p.feature_integrity(scenario.decisions, tca=scenario_run["tca"], graph=scenario_run["graph"],
                                  market_data=scenario.market_data, decision_session=scenario.decision_session)
    assert summary["venues_flagged"] == ["VENUE_C"]
    flagged = sorted(r["session"] for r in summary["rows"] if r["flagged"])
    assert flagged[0] == scenario.config.alias_change_session


def test_shadow_evaluation(scenario_run, scenario):
    p = scenario_run["pipeline"]
    shadow = p.shadow_evaluation(scenario.decisions, tca=scenario_run["tca"], graph=scenario_run["graph"],
                                 market_data=scenario.market_data, decision_session=scenario.decision_session,
                                 outcome_model=SimulatedOutcomeModel(scenario.config))
    assert shadow.meets_minimum_window and shadow.sessions == 20
    corrected = shadow.comparisons["integrity_corrected"]
    assert corrected["matched_decisions"] == len(scenario.decisions)
    assert corrected["significant_improvement"] and corrected["ci"][1] < 0
    assert set(corrected["strata"]["regime"]) == {"LOW_VOLATILITY", "NORMAL", "HIGH_VOLATILITY"}
    assert "SHADOW_ONLY" in shadow.to_dict()["mode"]
