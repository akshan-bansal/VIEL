from decimal import Decimal

from helpers import exec_msg, order_msg, run_flow

from venue_integrity.execution.execution_reconciler import ChildStatus, TradeStatus, reconcile_order_executions
from venue_integrity.schemas.enums import ExceptionType, MappingStatus


def exc_types(pipeline):
    return {r.exception_type for r in pipeline.exceptions.records()}


def test_partial_fills_accumulate():
    _, _, rep = run_flow([order_msg("C1", qty=500)],
                         [exec_msg("E1", "C1", qty=200, cum=200, seq=1), exec_msg("E2", "C1", qty=300, cum=500, seq=2,
                                                                                    ord_status="2")])
    child = rep.child("C1")
    assert child.filled_quantity == Decimal(500) and child.status is ChildStatus.FILLED and child.fill_count == 2


def test_duplicate_exec_id_counted_once():
    fill = exec_msg("E1", "C1", qty=200, cum=200, seq=1)
    # session-level retransmission: new header (PossDupFlag, SendingTime), same economic body
    resend = exec_msg("E1", "C1", qty=200, cum=200, seq=1, poss_dup=True, sending_offset_ms=50)
    p, _, rep = run_flow([order_msg("C1")], [fill, resend])
    assert rep.child("C1").filled_quantity == Decimal(200)
    assert rep.kpis["duplicate_messages_excluded"] == 1
    assert ExceptionType.DUPLICATE_EXEC_ID in exc_types(p)


def test_conflicting_exec_id_excluded_and_flagged():
    p, _, rep = run_flow([order_msg("C1")], [exec_msg("E1", "C1", qty=200, seq=1), exec_msg("E1", "C1", qty=300, seq=2)])
    assert rep.child("C1").filled_quantity == 0
    assert ExceptionType.CONFLICTING_EXEC_ID in exc_types(p)


def test_correction_chain_uses_latest_state():
    _, _, rep = run_flow([order_msg("C1")], [
        exec_msg("E1", "C1", qty=300, px="101.00", seq=1),
        exec_msg("E2", "C1", exec_type="G", ref="E1", qty=250, px="101.10", seq=2),
        exec_msg("E3", "C1", exec_type="G", ref="E2", qty=200, px="101.20", seq=3),
    ])
    (e,) = rep.executions
    assert (e.status, e.quantity, e.price, e.chain) == (TradeStatus.ACTIVE, Decimal(200), Decimal("101.20"), ("E1", "E2", "E3"))
    assert rep.child("C1").filled_quantity == Decimal(200)


def test_bust_removes_quantity_and_keeps_history():
    _, _, rep = run_flow([order_msg("C1")], [exec_msg("E1", "C1", qty=300, seq=1),
                                             exec_msg("E2", "C1", exec_type="H", ref="E1", qty=300, seq=2)])
    (e,) = rep.executions
    assert e.status is TradeStatus.BUSTED and e.quantity == 0 and e.chain == ("E1", "E2")
    assert rep.child("C1").filled_quantity == 0


def test_fix42_exec_trans_type_corrections():
    orders = [order_msg("C1", begin="FIX.4.2")]
    execs = [exec_msg("E1", "C1", exec_type="1", exec_trans_type="0", qty=300, begin="FIX.4.2", seq=1),
             exec_msg("E2", "C1", exec_type="1", exec_trans_type="2", ref="E1", qty=100, begin="FIX.4.2", seq=2),
             exec_msg("E3", "C1", exec_type="1", exec_trans_type="0", qty=50, begin="FIX.4.2", seq=3),
             exec_msg("E4", "C1", exec_type="1", exec_trans_type="1", ref="E3", qty=50, begin="FIX.4.2", seq=4)]
    _, _, rep = run_flow(orders, execs)
    assert rep.child("C1").filled_quantity == Decimal(100)
    assert {e.original_exec_id: e.status for e in rep.executions} == {"E1": TradeStatus.ACTIVE, "E3": TradeStatus.BUSTED}


def test_ambiguous_and_orphan_corrections():
    p, _, rep = run_flow([order_msg("C1")], [
        exec_msg("E1", "C1", qty=300, seq=1),
        exec_msg("E2", "C1", exec_type="G", ref="E1", qty=200, seq=2),
        exec_msg("E3", "C1", exec_type="G", ref="E1", qty=100, seq=3),
        exec_msg("E9", "C1", exec_type="H", ref="NOPE", qty=1, seq=4),
    ])
    assert rep.executions[0].status is TradeStatus.AMBIGUOUS and rep.child("C1").filled_quantity == 0
    assert {ExceptionType.AMBIGUOUS_CORRECTION, ExceptionType.ORPHAN_CORRECTION} <= exc_types(p)


def test_overfill_orphan_and_cancel():
    p, _, rep = run_flow([order_msg("C1", qty=100), order_msg("C2", qty=500, seq=2)], [
        exec_msg("E1", "C1", qty=150, seq=1),
        exec_msg("E2", "UNKNOWN", qty=10, seq=2),
        exec_msg("E3", "C2", qty=100, cum=100, seq=3),
        exec_msg("E4", "C2", exec_type="4", ord_status="4", qty=None, px=None, last_mkt=None, cum=100, seq=4),
    ])
    assert rep.child("C1").status is ChildStatus.OVERFILLED
    assert rep.child("C2").status is ChildStatus.PARTIALLY_FILLED_CANCELED
    assert {ExceptionType.OVERFILL, ExceptionType.ORPHAN_EXECUTION} <= exc_types(p)
    assert rep.kpis["lineage_exceptions"] == 1


def test_cumqty_mismatch_reveals_missing_fill():
    p, _, rep = run_flow([order_msg("C1")], [exec_msg("E2", "C1", qty=100, cum=300, seq=2)])
    assert ExceptionType.CUMQTY_MISMATCH in exc_types(p)


def test_intended_and_actual_venues_preserved_separately():
    p, n, rep = run_flow([order_msg("C1", dest="ABC")], [exec_msg("E1", "C1", last_mkt="XVNA", seq=1)])
    child = rep.child("C1")
    assert child.intended_venue_id == "VENUE_C" and child.venue_breakdown == {"VENUE_A": Decimal(100)}
    assert ExceptionType.DESTINATION_MISMATCH in exc_types(p)


def test_unresolved_actual_venue_is_never_backfilled_from_intent():
    p, n, rep = run_flow([order_msg("C1", dest="ABC")], [exec_msg("E1", "C1", last_mkt=None, seq=1)])
    (e,) = rep.executions
    assert e.actual_venue_id is None and e.mapping_status is MappingStatus.UNMAPPED and e.venue_key == "UNRESOLVED"
    assert e.intended_venue_id == "VENUE_C"
    assert rep.kpis["silent_venue_fallbacks"] == 0
    assert ExceptionType.UNRESOLVED_EXECUTION_VENUE in exc_types(p)


def test_dict_reconciliation_sketch():
    out = reconcile_order_executions(
        [{"child_order_id": "C1", "order_quantity": 500}, {"child_order_id": "C2", "order_quantity": 100}],
        [{"exec_id": "E1", "child_order_id": "C1", "executed_quantity": 300, "actual_venue_id": "VENUE_C"},
         {"exec_id": "E1", "child_order_id": "C1", "executed_quantity": 300, "actual_venue_id": "VENUE_C"},
         {"exec_id": "E2", "child_order_id": "C2", "executed_quantity": 150, "actual_venue_id": None}])
    assert [(o["status"], o["executed_quantity"]) for o in out] == [("PARTIALLY_FILLED", 300), ("OVERFILLED", 150)]
    assert out[1]["venues"] == ["UNRESOLVED"]
