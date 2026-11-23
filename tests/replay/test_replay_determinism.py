"""Spec §11.5 replay and determinism."""

import random

import pytest

from venue_integrity.pipeline import AuditPipeline
from venue_integrity.reports.audit_summary import digest


def run(config, captures, registry=None, version=None):
    p = AuditPipeline(config, registry=registry, mode="replay")
    p.ingest(captures)
    n = p.normalize(registry_version=version)
    return p, n, p.reconcile(n)


def test_replay_reconciles_and_is_deterministic(config, scenario, scenario_run):
    ref_p, ref_n, ref_r = scenario_run["pipeline"], scenario_run["normalized"], scenario_run["report"]
    p, n, r = run(config, scenario.captures)
    assert p.raw_store.count() == ref_p.raw_store.count()
    assert p.raw_store.verify_chain().head_hash == ref_p.raw_store.verify_chain().head_hash
    assert digest(r.economic_state()) == digest(ref_r.economic_state())
    assert [(o.event_id, o.classification, o.mapping_status) for o in n.orders] == \
           [(o.event_id, o.classification, o.mapping_status) for o in ref_n.orders]
    # exceptions: compare two fresh runs (the shared fixture's queue also receives other tests' findings)
    p2, _, _ = run(config, scenario.captures)
    assert p.exceptions.summary() == p2.exceptions.summary()
    assert [r.exception_id for r in p.exceptions.records()] == [r.exception_id for r in p2.exceptions.records()]


def test_shuffled_delivery_gives_same_economic_state(config, scenario, scenario_run):
    shuffled = scenario.captures[:]
    random.Random(42).shuffle(shuffled)
    _, n, r = run(config, shuffled)
    assert digest(r.economic_state()) == digest(scenario_run["report"].economic_state())
    assert sorted((o.event_id, o.classification) for o in n.orders) == \
           sorted((o.event_id, o.classification) for o in scenario_run["normalized"].orders)


def test_unresolved_events_remain_visible(scenario_run, scenario):
    p, n = scenario_run["pipeline"], scenario_run["normalized"]
    unresolved = {o.event_id for o in n.orders if o.raw_ex_destination and o.mapping_status.value != "MAPPED"}
    flagged = {r.source_event_id for r in p.exceptions.records()}
    assert unresolved and unresolved <= flagged
    planted = scenario.ground_truth["planted_faults"]
    assert len(unresolved) == len(planted["legacy_alias_orders"]) + len(planted["ambiguous_alias_orders"])


@pytest.mark.slow
def test_evidence_package_reproducible(tmp_path):
    from venue_integrity.demo import run_demo

    a = run_demo(tmp_path / "a", verbose=False)
    b = run_demo(tmp_path / "b", verbose=False)
    assert a.package["audit_execution_id"] == b.package["audit_execution_id"]
    assert a.package["package_digest"] == b.package["package_digest"]
    assert a.package["replay_determinism"]["deterministic"]
    gt = a.package["ground_truth_check"]
    assert all(gt.values()), {k: v for k, v in gt.items() if not v}
    steps = {s["step"]: s for s in a.steps}
    assert steps[2]["legacy_label"] == "LEGACY"
    assert steps[3]["classification"] == "MARKET" and steps[3]["mapping_status"] == "UNMAPPED"
    assert steps[4]["exemplar_after"]["intended_venue_id"] == "VENUE_C"
    assert steps[4]["raw_payload_hash_unchanged"] and steps[4]["raw_chain_head_unchanged"]
    assert steps[4]["historical_replay_v2_0_reproducible"] == 1.0
    assert steps[4]["classification_changes_due_to_registry_change"] == 0
    statuses = {r["kpi"]: r["status"] for r in a.package["kpi_scorecard"]}
    assert statuses["Silent venue fallback"] == statuses["Silent order-type coercion"] == "PASS"
    assert statuses["Historical decision reconstruction"] == "FAIL"  # planted router defects are reported, not hidden
