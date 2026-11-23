"""Spec §11.4 integration and failure-injection matrix, one test per row."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
from helpers import T0, capture, exec_msg, order_msg, run_flow

from venue_integrity.execution.execution_reconciler import TradeStatus
from venue_integrity.graph.centrality import calculate_influence
from venue_integrity.graph.lead_lag import estimate_lead_lag, shift_within_sessions
from venue_integrity.graph.network_builder import build_venue_graph
from venue_integrity.graph.validation import synthetic_mids
from venue_integrity.identity.mapping_history import MappingChangeManager
from venue_integrity.pipeline import AuditPipeline
from venue_integrity.router.decision_reconstruction import ReconstructionStatus, reconstruct_decision
from venue_integrity.router.venue_scoring import load_router_models, score_candidates, select_venue
from venue_integrity.schemas.enums import ExceptionType, MappingStatus, OrderType, Side
from venue_integrity.schemas.router_decision import CandidateFeatures, RouterDecision
from venue_integrity.tca.engine import TcaEngine
from venue_integrity.tca.markout import MarketData, QuoteSeries


def types(p):
    return {r.exception_type for r in p.exceptions.records()}


def test_new_venue_alias_raises_unmapped_exception():
    p, n, _ = run_flow([order_msg("C1", dest="ABC_ROUTER")], [])
    (o,) = n.orders
    assert o.mapping_status is MappingStatus.UNMAPPED and o.classification.value == "MARKET"
    assert ExceptionType.UNKNOWN_DESTINATION in types(p)


def test_multiple_aliases_raise_ambiguous_exception():
    p, n, _ = run_flow([order_msg("C1", dest="SMART")], [], source="BROKER_B")
    assert n.orders[0].mapping_status is MappingStatus.AMBIGUOUS and n.orders[0].intended_venue_id is None
    assert ExceptionType.AMBIGUOUS_VENUE in types(p)


def test_fix_tag_variation_is_validated_explicitly():
    p, n, rep = run_flow(
        [order_msg("C1", begin="FIX.4.2"), order_msg("C2", begin="FIX.5.0SP2", seq=2), order_msg("C3", seq=3, extra=[(8888, "v")])],
        [exec_msg("E1", "C1", exec_type="2", exec_trans_type="0", begin="FIX.4.2", qty=500, seq=1)])
    assert {o.cl_ord_id for o in n.orders} == {"C1", "C3"}  # unsupported protocol not normalised...
    assert p.raw_store.count() == 4  # ...but retained
    assert {ExceptionType.PROTOCOL_VALIDATION_FAILURE, ExceptionType.UNKNOWN_FIX_TAG} <= types(p)
    assert rep.child("C1").filled_quantity == Decimal(500)  # FIX 4.2 fill semantics understood


def test_duplicate_exec_id_not_double_counted():
    fill = exec_msg("E1", "C1", qty=200, seq=1)
    p, _, rep = run_flow([order_msg("C1")], [fill, fill, exec_msg("E1", "C1", qty=200, seq=1, poss_dup=True, sending_offset_ms=9)])
    assert rep.child("C1").filled_quantity == Decimal(200)
    assert p.ingestion.duplicate_deliveries == 1 and rep.kpis["duplicate_messages_excluded"] == 1


def test_partial_fills_cumulative_quantity():
    execs = [exec_msg(f"E{i}", "C1", qty=100, cum=100 * (i + 1), seq=i + 1, offset_ms=i) for i in range(4)]
    _, _, rep = run_flow([order_msg("C1", qty=500)], execs)
    child = rep.child("C1")
    assert child.filled_quantity == Decimal(400) and child.reported_cum_qty == Decimal(400)
    assert child.status.value == "PARTIALLY_FILLED"


def test_correction_and_bust_update_state_and_retain_history():
    p, _, rep = run_flow([order_msg("C1")], [
        exec_msg("E1", "C1", qty=300, seq=1), exec_msg("E2", "C1", qty=200, seq=2),
        exec_msg("E3", "C1", exec_type="G", ref="E1", qty=250, px="101.30", seq=3),
        exec_msg("E4", "C1", exec_type="H", ref="E2", qty=200, seq=4)])
    by_id = {e.original_exec_id: e for e in rep.executions}
    assert by_id["E1"].quantity == Decimal(250) and by_id["E1"].chain == ("E1", "E3")
    assert by_id["E2"].status is TradeStatus.BUSTED and by_id["E2"].chain == ("E2", "E4")
    assert rep.child("C1").filled_quantity == Decimal(250)
    assert p.raw_store.count() == 5  # every original report retained


def test_intended_actual_mismatch_preserves_both():
    p, _, rep = run_flow([order_msg("C1", dest="VNA")], [exec_msg("E1", "C1", last_mkt="XABC")])
    (e,) = rep.executions
    assert (e.intended_venue_id, e.actual_venue_id) == ("VENUE_A", "VENUE_C")
    assert ExceptionType.DESTINATION_MISMATCH in types(p)


def test_sequence_gap_exception_and_recovery():
    p = AuditPipeline(mode="replay")
    m1, m3 = exec_msg("E1", "C1", seq=1), exec_msg("E3", "C1", seq=3, offset_ms=3)
    m2 = exec_msg("E2", "C1", seq=2, offset_ms=2, poss_dup=True, sending_offset_ms=50)
    p.ingest([capture(m1, session="X", offset_ms=1), capture(m3, session="X", offset_ms=3)])
    assert p.sequence_monitor.open_gaps() == {"BROKER_A|X": [2]}
    assert ExceptionType.SEQUENCE_GAP in types(p)
    p.ingest([capture(m2, session="X", offset_ms=60)])
    assert p.sequence_monitor.open_gaps() == {}
    assert p.ingestion.sequence_outcomes.get("GAP_RECOVERED") == 1


def test_clock_drift_flagged():
    p = AuditPipeline(mode="replay")
    p.ingest([capture(order_msg("C1"), offset_ms=4500)])
    (rec,) = p.exceptions.filter(exception_type=ExceptionType.CLOCK_DRIFT)
    assert rec.context["lag_ms"] > 4000


def _market(ts_ms, mids):
    ns = 1_000_000
    t0 = int(T0.timestamp() * 1e9)
    ts = np.array([t0 + int(t * ns) for t in ts_ms], dtype=np.int64)
    mids = np.asarray(mids, dtype=float)
    series = QuoteSeries(ts, mids - 0.01, mids + 0.01, windows=[(t0, t0 + 60_000 * ns)])
    return MarketData({"INSTRUMENT_001": series}, ts, {}, np.zeros(len(ts)), 100 * ns)


def test_stale_quote_excluded_and_flagged():
    _, _, rep = run_flow([order_msg("C1")], [exec_msg("E1", "C1", offset_ms=5000), exec_msg("E2", "C1", seq=2, offset_ms=100)])
    md = _market([0, 50, 6000], [101.0, 101.0, 101.0])  # nothing between 50 ms and 6 s
    tca = TcaEngine(md, horizons_ms=[100, 1000], reference_horizon_ms=1000, max_staleness_ms=1000,
                    fee_schedule_bps={"VENUE_C": 0.3}).run(rep)
    by_id = {f.exec_id: f for f in tca.fills}
    assert not by_id["E1"].included and by_id["E1"].exclusion_reason == "STALE_MIDPOINT_AT_EXECUTION"
    assert by_id["E2"].included and by_id["E2"].horizons[1000].status.value == "STALE"  # flagged, not zero
    assert by_id["E2"].horizons[1000].adverse_selection_bps is None
    assert tca.coverage["stale_midpoints_included"] == 0
    assert any(f.exception_type is ExceptionType.STALE_MIDPOINT for f in tca.findings)


def test_graph_edge_reversal_changes_influence():
    mids, sess, _ = synthetic_mids({"A": 1, "C": 0}, ticks=20_000)
    forward = estimate_lead_lag(mids, sess)
    reversed_mids = {"A": mids["A"], "C": shift_within_sessions(mids["C"], 3, sess)}
    backward = estimate_lead_lag(reversed_mids, sess)
    f = calculate_influence(build_venue_graph(forward.edge_records(), forward.venues)).scores
    b = calculate_influence(build_venue_graph(backward.edge_records(), backward.venues)).scores
    assert f["C"] == 1.0 and f["A"] == 0.0
    assert b["A"] == 1.0 and b["C"] == 0.0


def test_mapping_update_keeps_historical_replay_reproducible(registry, mic_reference):
    orders = [order_msg("C1", dest="ABC_ROUTER")]
    p, before, _ = run_flow(orders, [])
    mgr = MappingChangeManager(registry, mic_reference)
    req = mgr.propose(proposed_by="maker", description="add", evidence="notes", at=T0,
                      add_records=[{"venue_id": "VENUE_C", "mic": "XABC", "broker_aliases": {"BROKER_A": ["ABC_ROUTER"]},
                                    "venue_type": "EXCHANGE", "valid_from": "2026-11-04T00:00:00Z"}])
    new, _ = mgr.approve(req.request_id, approved_by="checker", new_version="2.1", at=T0)
    after = p.normalize(registry=new)
    replay = p.normalize(registry=new, registry_version="2.0")
    assert after.orders[0].intended_venue_id == "VENUE_C"
    assert replay.orders[0].model_dump(exclude={"normalized_at"}) == before.orders[0].model_dump(exclude={"normalized_at"})
    assert after.orders[0].classification == before.orders[0].classification


def test_missing_model_feature_explicit_fallback_and_record(config, registry):
    models = load_router_models(config.model_weights)
    t = datetime(2026, 11, 5, 14, 30, tzinfo=UTC)

    def f(**kw):
        base = dict(fill_probability=0.95, liquidity=1.0, price_score=1.0, adverse_selection_bps=1.0,
                    market_impact_bps=0.4, transaction_cost_bps=1.5, graph_influence=0.3,
                    feature_timestamp=t - timedelta(seconds=1), feature_model_version="1.0")
        return CandidateFeatures(**{**base, **kw})

    feats = {"VENUE_A": f(adverse_selection_bps=5.0, graph_influence=None), "VENUE_C": f()}
    choice = select_venue(score_candidates(feats, models["1.4"]), models["1.4"])
    d = RouterDecision(decision_id="D1", parent_order_id="P1", child_order_id="C1", decision_timestamp=t,
                       instrument_id="INSTRUMENT_001", side=Side.BUY, order_type=OrderType.MARKET, quantity=Decimal(500),
                       candidate_venues=("VENUE_A", "VENUE_C"), decision_features=feats, selected_venue=choice,
                       router_score=score_candidates(feats, models["1.4"])[choice], router_model_version="1.4",
                       reference_data_version="2.0", decision_reason="HIGHEST_EXPECTED_UTILITY",
                       fallbacks_applied=("VENUE_A:adverse_selection_bps=APPROVED_DEFAULT_5.0",))
    result = reconstruct_decision(d, models, registry=registry)
    assert result.status is ReconstructionStatus.REPRODUCED
    assert any("fallback" in i for i in result.issues) and any("graph_influence missing" in i for i in result.issues)
    assert any(x.exception_type is ExceptionType.MISSING_MODEL_FEATURE for x in result.findings)
    # an unrecorded missing feature makes the decision non-reproducible from its record
    bad = d.model_copy(update={"fallbacks_applied": (), "decision_features": {**feats, "VENUE_A": f(adverse_selection_bps=None)}})
    assert reconstruct_decision(bad, models, registry=registry).status is not ReconstructionStatus.REPRODUCED
