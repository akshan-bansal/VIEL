"""FIX ingestion through reconciliation, lineage and the CLI."""

import json

from helpers import exec_msg, order_msg, run_flow

from venue_integrity.cli import main
from venue_integrity.ingestion.sources import write_captures


def test_end_to_end_handcrafted_flow():
    orders = [order_msg("C1", dest="ABC", qty=500, parent="P1"), order_msg("C2", dest="VNA", qty=300, parent="P1", seq=2),
              order_msg("C3", dest="ABC_ROUTER", qty=200, parent="P2", seq=3)]
    execs = [exec_msg("E1", "C1", qty=500, cum=500, ord_status="2", seq=1),
             exec_msg("E2", "C2", qty=100, last_mkt="XVNA", cum=100, seq=2, order_qty=300),
             exec_msg("E3", "C3", qty=200, last_mkt=None, cum=200, seq=3, order_qty=200, parent="P2")]
    p, n, rep = run_flow(orders, execs)
    assert [o.classification.value for o in sorted(n.orders, key=lambda o: o.cl_ord_id)] == ["MARKET"] * 3
    assert {c.child_order_id: c.status.value for c in rep.children} == {
        "C1": "FILLED", "C2": "PARTIALLY_FILLED", "C3": "FILLED"}
    lineage = p.lineage(n, rep)
    trace = lineage.trace("E1", "BROKER_A")
    assert trace.complete and trace.payload_hashes_verified and trace.parent_order_id == "P1"
    tree = lineage.tree("P1")
    assert [c["child_order_id"] for c in tree["children"]] == ["C1", "C2"]
    assert "Execution E1" in lineage.render_tree(tree)
    assert rep.kpis["unresolved_venue_quantity"] == "200"


def test_cli_replay_resolve_and_benchmarks(tmp_path, capsys, scenario):
    path = tmp_path / "caps.jsonl"
    write_captures(path, scenario.captures[:400])
    out = tmp_path / "summary.json"
    assert main(["replay", str(path), "--out", str(out)]) == 0
    summary = json.loads(out.read_text())
    assert summary["raw_store"]["chain_ok"] and summary["ingestion"]["captures"] == 400
    capsys.readouterr()
    assert main(["resolve", "ABC", "--event-time", "2026-11-10T14:30:00Z"]) == 0
    assert json.loads(capsys.readouterr().out)["venue_id"] == "VENUE_C"
    assert main(["resolve", "ABC_ROUTER", "--event-time", "2026-11-10T14:30:00Z", "--source-system", "BROKER_A"]) == 2
    capsys.readouterr()
    assert main(["benchmarks"]) == 0
