"""Prototype acceptance demonstration (spec §19), simulation mode.

1. Inject a market order with an unrecognised venue identifier.
2. Show how the legacy fallback classified it as LEGACY.
3. Run the new resolver: the order stays MARKET with UNMAPPED venue status.
4. Register the correct venue (maker-checker) and reprocess the original raw
   event without changing its payload; historical replay stays reproducible.
5. Reconstruct its parent-child-execution lineage.
6. Calculate venue-specific spread and adverse-selection metrics.
7. Feed corrected observations into the graph and SOR audit layers.
8. Generate an audit report showing the defect, the correction and its impact.

The demonstration runs entirely on synthetic data. It never connects to an
order-routing session and never issues trading instructions.
"""

from __future__ import annotations

import random
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from venue_integrity.analytics.performance_attribution import attribute_shortfall
from venue_integrity.config import load_config
from venue_integrity.execution.execution_reconciler import ReconciliationReport
from venue_integrity.execution.legacy_classifier import LEGACY_LABEL, LegacyNormalizer, legacy_alias_table
from venue_integrity.execution.order_normalizer import classify_order, normalize_order_type
from venue_integrity.graph.centrality import calculate_influence
from venue_integrity.graph.network_builder import build_venue_graph
from venue_integrity.graph.validation import run_graph_benchmarks
from venue_integrity.identity.mapping_history import MakerCheckerError, MappingChangeManager
from venue_integrity.ingestion.raw_event_store import RawEventStore
from venue_integrity.ingestion.sources import read_captures, write_captures
from venue_integrity.pipeline import AuditPipeline, NormalizationResult
from venue_integrity.reports.audit_summary import (
    build_package,
    digest,
    file_manifest,
    go_live_gates,
    kpi_scorecard,
    write_package,
)
from venue_integrity.reports.tca_report import venue_tca_table
from venue_integrity.router.decision_reconstruction import ReconstructionStatus
from venue_integrity.schemas.enums import DefectCategory, ExceptionType, MappingStatus
from venue_integrity.schemas.venue_intelligence import SCORE_DEFINITIONS
from venue_integrity.simulation.generator import generate_scenario
from venue_integrity.simulation.outcome_model import SimulatedOutcomeModel
from venue_integrity.tca.engine import TcaEngine

MAKER = "refdata.analyst"
CHECKER = "refdata.controller"


@dataclass
class DemoResult:
    out_dir: Path
    package: dict[str, Any]
    markdown: str
    steps: list[dict[str, Any]] = field(default_factory=list)


def _p(verbose: bool, *lines: str) -> None:
    if verbose:
        for line in lines:
            print(line)


def _mapping_signature(n: NormalizationResult) -> dict[str, tuple]:
    sig = {o.event_id: (o.mapping_status.value, o.intended_venue_id, o.classification.value) for o in n.orders}
    sig.update({e.source_event_id: (e.mapping_status.value, e.actual_venue_id) for e in n.executions})
    return sig


def run_demo(out_dir: str | Path = "audit_output", *, verbose: bool = True, run_tests: bool = False) -> DemoResult:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    config = load_config()
    steps: list[dict[str, Any]] = []

    # ------------------------------------------------------------ inputs
    bundle = generate_scenario()
    capture_path = out / "raw_capture.jsonl"
    write_captures(capture_path, bundle.captures)
    store_path = out / "raw_events.sqlite"
    if store_path.exists():
        store_path.unlink()
    pipeline = AuditPipeline(config, raw_store=RawEventStore(store_path), mode="simulation")
    captures = list(read_captures(capture_path))
    ingestion = pipeline.ingest(captures)
    chain_before = pipeline.raw_store.verify_chain()
    original_version = pipeline.registry.latest_version
    _p(verbose, "=" * 78, "VENUE INTEGRITY AUDIT — prototype acceptance demonstration (simulation mode)", "=" * 78,
       f"captured messages: {ingestion.captures}  stored: {ingestion.stored}  duplicate deliveries: "
       f"{ingestion.duplicate_deliveries}  invalid: {ingestion.invalid}",
       f"raw hash chain: ok={chain_before.ok} head={chain_before.head_hash[:16]}…")

    # ------------------------------------------------------------ step 1
    before = pipeline.normalize()
    exemplar = next(o for o in before.orders if o.raw_ex_destination == bundle.config.new_alias and o.raw_ord_type == "1")
    raw_exemplar = pipeline.raw_store.get(exemplar.event_id)
    steps.append({"step": 1, "title": "Market order with an unrecognised venue identifier",
                  "event_id": exemplar.event_id, "cl_ord_id": exemplar.cl_ord_id,
                  "raw_payload": raw_exemplar.raw_payload.replace("\x01", "|"),
                  "payload_hash": raw_exemplar.payload_hash})
    _p(verbose, "", "[1] Market order routed through an alias registry v2.0 does not know",
       f"    {raw_exemplar.raw_payload.replace(chr(1), '|')}",
       f"    OrdType(40)={exemplar.raw_ord_type}  ExDestination(100)={exemplar.raw_ex_destination}")

    # ------------------------------------------------------------ step 2
    legacy = LegacyNormalizer(legacy_alias_table(pipeline.registry, original_version))
    legacy_label = legacy.classify(exemplar.raw_ord_type, exemplar.raw_ex_destination)
    legacy_counts = Counter((o.raw_ord_type, legacy.classify(o.raw_ord_type, o.raw_ex_destination)) for o in before.orders)
    market_legacy = legacy_counts.get(("1", LEGACY_LABEL), 0)
    smart_silent = sum(1 for o in before.orders if o.raw_ex_destination == "SMART")
    steps.append({"step": 2, "title": "Legacy fallback", "legacy_label": legacy_label,
                  "market_orders_labelled_legacy": market_legacy,
                  "ambiguous_alias_silently_resolved": smart_silent,
                  "legacy_table_SMART": legacy.alias_table.get("SMART")})
    _p(verbose, "", "[2] Legacy normaliser (reconstructed defect)",
       f"    exemplar -> {legacy_label!r}; {market_legacy} market orders labelled LEGACY in total",
       f"    also: ambiguous alias 'SMART' silently resolved to {legacy.alias_table.get('SMART')} for {smart_silent} orders")

    # ------------------------------------------------------------ step 3
    exc = next(r for r in pipeline.exceptions.filter(exception_type=ExceptionType.UNKNOWN_DESTINATION)
               if r.source_event_id == exemplar.event_id)
    steps.append({"step": 3, "title": "New resolver", "order_type": exemplar.order_type.value,
                  "classification": exemplar.classification.value, "mapping_status": exemplar.mapping_status.value,
                  "intended_venue_id": exemplar.intended_venue_id, "mapping_reason": exemplar.mapping_reason,
                  "exception_id": exc.exception_id, "exception_severity": exc.severity.value,
                  "alert": exc.alert_code.value if exc.alert_code else None})
    _p(verbose, "", "[3] New resolver + normaliser",
       f"    order_type={exemplar.order_type}  classification={exemplar.classification}  "
       f"mapping_status={exemplar.mapping_status} ({exemplar.mapping_reason})  intended_venue_id={exemplar.intended_venue_id}",
       f"    exception {exc.exception_type} [{exc.severity}] alert {exc.alert_code} -> '{exc.required_action}'")

    # ------------------------------------------------------------ step 4
    manager = MappingChangeManager(pipeline.registry, pipeline.mic_reference)
    at = bundle.config.session_start(bundle.config.sessions - 1) + timedelta(hours=8)
    request = manager.propose(
        proposed_by=MAKER, at=at,
        description=f"Register BROKER_A destination alias {bundle.config.new_alias} for VENUE_C",
        evidence="BROKER_A routing release notes (2026-11-04): ABC_ROUTER routes to venue XABC; "
                 "confirmed against LastMkt=XABC on the resulting fills",
        add_records=[{"venue_id": "VENUE_C", "mic": "XABC", "operating_mic": "XABC", "raw_aliases": [],
                      "broker_aliases": {"BROKER_A": [bundle.config.new_alias]},
                      "venue_name": "Example Execution Venue", "venue_type": "EXCHANGE", "country": "CA",
                      "valid_from": bundle.config.new_alias_valid_from, "valid_to": None, "status": "ACTIVE"}],
    )
    try:
        manager.approve(request.request_id, approved_by=MAKER, new_version="2.1", at=at)
        maker_checker_enforced = False
    except MakerCheckerError:
        maker_checker_enforced = True
    _, finding_21 = manager.approve(request.request_id, approved_by=CHECKER, new_version="2.1", at=at + timedelta(minutes=5))
    smart_d = next(r for r in manager.registry.records_at(None)
                   if r.venue_id == "VENUE_D" and "SMART" in r.broker_aliases.get("BROKER_B", ()))
    request2 = manager.propose(
        proposed_by=MAKER, at=at + timedelta(minutes=10),
        description="Withdraw erroneous BROKER_B 'SMART' registration against VENUE_D",
        evidence="BROKER_B confirmation: SMART is its smart-router destination for VENUE_A only",
        supersede=[smart_d],
    )
    registry, finding_22 = manager.approve(request2.request_id, approved_by=CHECKER, new_version="2.2",
                                           at=at + timedelta(minutes=15))
    pipeline.exceptions.raise_all([finding_21, finding_22], at + timedelta(minutes=15))

    after = pipeline.normalize(registry=registry, raise_exceptions=False)
    replay_v20 = pipeline.normalize(registry=registry, registry_version=original_version, raise_exceptions=False)
    chain_after = pipeline.raw_store.verify_chain()
    exemplar_after = next(o for o in after.orders if o.event_id == exemplar.event_id)
    sig_before, sig_replay = _mapping_signature(before), _mapping_signature(replay_v20)
    reproducible = sum(1 for k in sig_before if sig_replay.get(k) == sig_before[k]) / len(sig_before)
    classification_changes = sum(
        1 for o_before, o_after in zip(sorted(before.orders, key=lambda o: o.event_id),
                                       sorted(after.orders, key=lambda o: o.event_id), strict=True)
        if o_before.classification != o_after.classification
    )

    resolved = 0
    for record in pipeline.exceptions.unresolved():
        if record.exception_type not in (ExceptionType.UNKNOWN_DESTINATION, ExceptionType.AMBIGUOUS_VENUE):
            continue
        order = next((o for o in after.orders if o.event_id == record.source_event_id), None)
        if order is None or order.mapping_status is not MappingStatus.MAPPED:
            continue
        request_id = request.request_id if record.raw_value == bundle.config.new_alias else request2.request_id
        pipeline.exceptions.assign(record.exception_id, MAKER, at, by=CHECKER)
        pipeline.exceptions.resolve(record.exception_id, DefectCategory.VENUE_REFERENCE_DATA,
                                    f"change request {request_id} approved by {CHECKER}", at, by=MAKER)
        pipeline.exceptions.mark_reprocessed(record.exception_id,
                                             f"reprocessed under registry {registry.latest_version}: "
                                             f"{order.intended_venue_id}", at, by="system")
        resolved += 1
    steps.append({"step": 4, "title": "Register venue and reprocess", "change_requests": [request.to_dict(), request2.to_dict()],
                  "maker_checker_enforced": maker_checker_enforced, "registry_versions": [v.version for v in registry.versions],
                  "exemplar_after": {"mapping_status": exemplar_after.mapping_status.value,
                                     "intended_venue_id": exemplar_after.intended_venue_id,
                                     "classification": exemplar_after.classification.value,
                                     "mapping_version": exemplar_after.mapping_version},
                  "raw_payload_hash_unchanged": pipeline.raw_store.get(exemplar.event_id).payload_hash == raw_exemplar.payload_hash,
                  "raw_chain_head_unchanged": chain_before.head_hash == chain_after.head_hash,
                  "historical_replay_v2_0_reproducible": reproducible,
                  "classification_changes_due_to_registry_change": classification_changes,
                  "exceptions_resolved_and_reprocessed": resolved})
    _p(verbose, "", "[4] Reference-data fix (maker-checker) and reprocessing from the raw store",
       f"    self-approval rejected: {maker_checker_enforced}; registry versions {[v.version for v in registry.versions]}",
       f"    exemplar under {exemplar_after.mapping_version}: {exemplar_after.mapping_status} -> {exemplar_after.intended_venue_id}, "
       f"classification still {exemplar_after.classification}",
       f"    payload hash unchanged: {steps[-1]['raw_payload_hash_unchanged']}; chain head unchanged: "
       f"{steps[-1]['raw_chain_head_unchanged']}",
       f"    replay under v2.0 reproduces original mapping for {reproducible:.2%} of events; "
       f"classification changes caused by the registry change: {classification_changes}",
       f"    {resolved} exceptions resolved (root cause VENUE_REFERENCE_DATA) and reprocessed")

    # ------------------------------------------------------------ step 5
    report: ReconciliationReport = pipeline.reconcile(after)
    lineage = pipeline.lineage(after, report)
    tree = lineage.tree(exemplar.parent_order_id)
    exemplar_exec = next((e for e in report.executions if e.child_order_id == exemplar.cl_ord_id), None)
    trace = lineage.trace(exemplar_exec.original_exec_id, exemplar_exec.source_system) if exemplar_exec else None
    steps.append({"step": 5, "title": "Lineage", "tree": tree, "trace": trace.to_dict() if trace else None})
    _p(verbose, "", "[5] Parent-child-execution lineage", *("    " + line for line in lineage.render_tree(tree).splitlines()),
       f"    trace {trace.exec_id}: complete={trace.complete} payload hashes verified={trace.payload_hashes_verified}"
       if trace else "    (no execution)")

    # ------------------------------------------------------------ step 6
    tca = pipeline.run_tca(bundle.market_data, report)
    legacy_children = {o.cl_ord_id for o in before.orders if legacy.classify(o.raw_ord_type, o.raw_ex_destination) == LEGACY_LABEL}
    production_view = _production_view_tca(pipeline, bundle.market_data, report, legacy_children)
    ref = tca.reference_horizon_ms
    c_corr = tca.venues.get("VENUE_C")
    c_prod = production_view.venues.get("VENUE_C")
    steps.append({"step": 6, "title": "Venue TCA", "tca": tca.to_dict(),
                  "production_view_tca": production_view.to_dict(),
                  "shortfall_attribution": attribute_shortfall(tca)})
    _p(verbose, "", "[6] Venue-specific TCA (actual execution venue; unresolved kept separate)",
       venue_tca_table(tca.to_dict(), ref),
       f"    VENUE_C as production venue analytics saw it (LEGACY bucket excluded): {c_prod.fills if c_prod else 0} "
       f"fills ({(c_prod.fills if c_prod else 0) / c_corr.fills:.0%} of its actual fills), "
       f"AS@{ref}ms={_fmt(c_prod.as_bps(ref) if c_prod else None)} bps",
       f"    VENUE_C with corrected identity: {c_corr.fills} fills, AS@{ref}ms={_fmt(c_corr.as_bps(ref))} bps")

    # ------------------------------------------------------------ step 7
    graph = pipeline.run_graph(bundle.market_data)
    naive_graph = pipeline.run_graph(bundle.market_data, latency_adjust=False)
    benchmarks = run_graph_benchmarks()
    venue_intel, _ = pipeline.venue_intelligence(tca, graph, bundle.market_data, report,
                                                 calculated_at=at)
    stage_records = pipeline.legacy_stage_records(before, original_version)
    localisation = pipeline.localise_defect(stage_records)
    contamination = pipeline.contamination(before, original_version, registry, graph.influence.scores,
                                           bundle.session_windows, bundle.change_events, report)
    reconstructions = pipeline.reconstruct_decisions(bundle.decisions, report, tca)
    feature_audit = pipeline.feature_integrity(bundle.decisions, tca=tca, graph=graph, market_data=bundle.market_data,
                                               decision_session=bundle.decision_session)
    c_rows = [r for r in feature_audit["rows"] if r["venue"] == "VENUE_C"]
    first_flag = min((r["session"] for r in c_rows if r["flagged"]), default=None)
    shadow = pipeline.shadow_evaluation(bundle.decisions, tca=tca, graph=graph, market_data=bundle.market_data,
                                        decision_session=bundle.decision_session,
                                        outcome_model=SimulatedOutcomeModel(bundle.config))
    recon_counts = Counter(r.status.value for r in reconstructions)
    steps.append({"step": 7, "title": "Graph and SOR audit"})
    _p(verbose, "", "[7] Graph intelligence and SOR audit",
       "    lead-lag edges (i -> j: i leads j): " + ", ".join(
           f"{e.source_venue}->{e.target_venue} ({e.net_lead:.2f})" for e in graph.lead_lag.edges),
       "    influence: " + ", ".join(f"{k}={v:.2f}" for k, v in sorted(graph.influence.scores.items())),
       "    without feed-latency adjustment: " + ", ".join(
           f"{e.source_venue}->{e.target_venue}" for e in naive_graph.lead_lag.edges),
       f"    graph benchmarks passed: {sum(b.passed for b in benchmarks)}/{len(benchmarks)}",
       "    venue risk (influence excluded): " + ", ".join(
           f"{v.venue_id}={_fmt(v.venue_risk)}" for v in venue_intel if v.observations),
       f"    defect localisation: {localisation['conclusion']}",
       "    legacy share by session: " + " ".join(f"{row['legacy_share']:.2f}" for row in contamination.series),
       "    change points: " + "; ".join(f"{c['label']} ({c['method']})" for c in contamination.change_points),
       f"    LEGACY bucket concentration: {contamination.concentration['by_corrected_venue']}",
       f"    decision reconstruction: {dict(recon_counts)}",
       f"    router feature integrity: venues flagged {feature_audit['venues_flagged']}; VENUE_C first flagged in "
       f"session {first_flag}",
       "    shadow ΔIS vs baseline (bps): " + ", ".join(
           f"{k}={v['mean_delta_is_bps']:+.3f} CI[{v['ci'][0]:+.3f},{v['ci'][1]:+.3f}]" for k, v in shadow.comparisons.items()))

    # ------------------------------------------------------------ step 8
    test_results = _run_tests() if run_tests else {"status": "NOT_RUN", "note": "run `via demo --with-tests` or pytest"}
    replay_check = _replay_determinism(config, capture_path, registry, report)
    metrics = _metrics(pipeline, ingestion, after, report, tca, graph, naive_graph, benchmarks, venue_intel,
                       bundle, reconstructions, shadow, reproducible, classification_changes, replay_check, test_results)
    scorecard = kpi_scorecard(metrics)
    gates = go_live_gates(metrics, scorecard)

    source_manifest = {"captures": file_manifest({"raw_capture.jsonl": capture_path}),
                       "raw_store": pipeline.raw_store.manifest(), "scenario_seed": bundle.config.seed}
    config_manifest = file_manifest(config.files)
    snapshot = registry.snapshot()
    sections = {
        "mode": "simulation",
        "source_data_manifest": source_manifest,
        "raw_event_counts_and_hashes": {**ingestion.to_dict(), "chain_head_hash": chain_after.head_hash,
                                         "chain_ok": chain_after.ok},
        "reference_data_snapshot": {"current": {k: v for k, v in snapshot.items() if k != "records"},
                                    "records": snapshot["records"],
                                    "audited_version": original_version,
                                    "mic_reference": {"source": Path(pipeline.mic_reference.source).name,
                                                      "sha256": pipeline.mic_reference.snapshot_hash},
                                    "change_requests": [r.to_dict() for r in manager.requests()]},
        "schema_and_parser_versions": {"schema_version": config.settings.ingestion["schema_version"],
                                       "parser_version": config.settings.ingestion["parser_version"],
                                       "fix_dictionary_version": pipeline.dictionary.version,
                                       "normalizer_version": exemplar.normalizer_version,
                                       "config_files": config_manifest},
        "exception_report": {"summary": pipeline.exceptions.summary(),
                             "unresolved_by_type_and_raw_value": _unresolved_groups(pipeline),
                             "alerts": pipeline.alerts.counts()},
        "execution_reconciliation_report": {"kpis": report.kpis,
                                            "exemplar_lineage": steps[4]},
        "graph_model_output": {"lead_lag": graph.lead_lag.to_dict(), "influence": graph.influence.to_dict(),
                               "without_latency_adjustment": [f"{e.source_venue}->{e.target_venue}"
                                                              for e in naive_graph.lead_lag.edges],
                               "benchmarks": [b.to_dict() for b in benchmarks],
                               "session_regimes": {str(k): v.value for k, v in graph.regimes.items()},
                               "venue_intelligence": [v.model_dump(mode="json") for v in venue_intel],
                               "score_definitions": SCORE_DEFINITIONS},
        "tca_report": steps[5]["tca"] | {"production_view_venue_c": production_view.to_dict()["venues"].get("VENUE_C"),
                                         "shortfall_attribution": steps[5]["shortfall_attribution"]},
        "classification_defect": {"localisation": localisation, "contamination": contamination.to_dict(),
                                  "legacy_demonstration": steps[1], "resolver_demonstration": steps[2],
                                  "remediation": steps[3]},
        "sor_decision_reconstruction": {"status_counts": dict(sorted(recon_counts.items())),
                                        "non_reproduced": [r.to_dict() for r in reconstructions
                                                           if r.status is not ReconstructionStatus.REPRODUCED],
                                        "decisions_with_fallbacks": [r.decision_id for r in reconstructions
                                                                     if any("fallback" in i for i in r.issues)],
                                        "feature_integrity": feature_audit,
                                        "shadow_evaluation": shadow.to_dict()},
        "test_results": test_results,
        "replay_determinism": replay_check,
        "kpi_scorecard": scorecard,
        "go_live_gates": gates,
        "approvals_and_remediation": {"reference_data_changes": manager.audit_log,
                                      "exceptions_reprocessed": resolved,
                                      "pending_sign_offs": ["SOR shadow results (Gate 5)",
                                                            "Operational governance (Gate 6)"]},
        "ground_truth_check": _ground_truth_check(bundle, pipeline, report),
    }
    package = build_package(sections, source_manifest=source_manifest,
                            reference_snapshot_hash=snapshot["snapshot_hash"], config_manifest=config_manifest)
    write_package(package, out / "audit_evidence.json")
    markdown = render_markdown(package, steps, tca_table=venue_tca_table(tca.to_dict(), ref))
    (out / "audit_report.md").write_text(markdown, encoding="utf-8")
    _p(verbose, "", "[8] Audit evidence package",
       f"    audit_execution_id={package['audit_execution_id']}",
       f"    package_digest={package['package_digest']}",
       f"    KPIs: {Counter(r['status'] for r in scorecard)}",
       "    gates: " + "; ".join(f"{g['gate']}={g['status']}" for g in gates),
       f"    written: {out / 'audit_evidence.json'}, {out / 'audit_report.md'}")
    pipeline.raw_store.close()
    return DemoResult(out, package, markdown, steps)


def _fmt(value) -> str:
    return "—" if value is None else f"{value:.3f}"


def _production_view_tca(pipeline: AuditPipeline, md, report: ReconciliationReport, legacy_children: set[str]):
    """What venue analytics looked like when the LEGACY bucket was excluded from venue statistics."""
    from dataclasses import replace

    kept = [e for e in report.executions if e.child_order_id not in legacy_children]
    view = replace(report, executions=kept)
    tca_cfg = pipeline.settings.tca
    engine = TcaEngine(md, horizons_ms=[int(h) for h in tca_cfg["horizons_ms"]],
                       reference_horizon_ms=int(tca_cfg["reference_horizon_ms"]),
                       max_staleness_ms=int(tca_cfg["max_midpoint_staleness_ms"]),
                       fee_schedule_bps={k: float(v) for k, v in tca_cfg["fee_schedule_bps"].items()})
    return engine.run(view)


def _unresolved_groups(pipeline: AuditPipeline) -> list[dict[str, Any]]:
    groups = Counter((r.exception_type.value, r.severity.value, r.raw_value) for r in pipeline.exceptions.unresolved())
    return [{"exception_type": t, "severity": s, "raw_value": v, "count": n}
            for (t, s, v), n in sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))]


def _replay_determinism(config, capture_path: Path, registry, original: ReconciliationReport) -> dict[str, Any]:
    """Replay the capture file through a fresh pipeline in shuffled delivery order."""
    captures = list(read_captures(capture_path))
    shuffled = captures[:]
    random.Random(1).shuffle(shuffled)
    results = {}
    for name, batch in (("in_order", captures), ("shuffled", shuffled)):
        p = AuditPipeline(config, registry=registry, mode="replay")
        p.ingest(batch)
        n = p.normalize(raise_exceptions=False)
        r = p.reconcile(n, raise_exceptions=False)
        results[name] = {
            "raw_events": p.raw_store.count(),
            "economic_state_digest": digest(r.economic_state()),
            "classification_digest": digest(sorted((o.event_id, o.classification.value) for o in n.orders)),
        }
    reference = digest(original.economic_state())
    return {
        **results,
        "reference_economic_state_digest": reference,
        "deterministic": len({v["economic_state_digest"] for v in results.values()} | {reference}) == 1
        and results["in_order"]["classification_digest"] == results["shuffled"]["classification_digest"],
    }


def _run_tests() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-m", "not slow", str(root / "tests")],
                          capture_output=True, text=True, cwd=root)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return {"status": "PASSED" if proc.returncode == 0 else "FAILED", "summary": tail, "returncode": proc.returncode}


def _metrics(pipeline, ingestion, after, report, tca, graph, naive_graph, benchmarks, venue_intel, bundle,
             reconstructions, shadow, reproducible, classification_changes, replay_check, test_results) -> dict[str, Any]:
    exc = pipeline.exceptions
    flagged_events = {r.source_event_id for r in exc.records()}
    unresolved_events = [o.event_id for o in after.orders if o.mapping_status is not MappingStatus.MAPPED and o.raw_ex_destination]
    unresolved_events += [e.source_event_id for e in after.executions
                          if e.exec_type.is_economic and e.mapping_status is not MappingStatus.MAPPED]
    monitored = sum(1 for e in unresolved_events if e in flagged_events) / len(unresolved_events) if unresolved_events else 1.0
    # supported identifiers: registered for a venue whose MIC is valid in the authoritative reference
    def _supported(reason: str) -> bool:
        return reason not in ("NO_MATCHING_IDENTIFIER", "MISSING_IDENTIFIER") and "MIC_" not in reason
    supported = [o.mapping_status for o in after.orders if o.raw_ex_destination and _supported(o.mapping_reason)]
    supported += [e.mapping_status for e in after.executions
                  if e.exec_type.is_economic and e.raw_last_mkt and _supported(e.mapping_reason.split(";")[-1].split(":")[-1])]
    supported_ok = [s for s in supported if s is MappingStatus.MAPPED]
    coercions = sum(1 for o in after.orders if o.classification is not classify_order(normalize_order_type(o.raw_ord_type)))
    coercions += classification_changes
    lineage_exc = report.kpis["lineage_exceptions"]
    lineage_flagged = len(exc.filter(exception_type=ExceptionType.ORPHAN_EXECUTION)) + len(
        exc.filter(exception_type=ExceptionType.MISSING_LINEAGE))
    unresolved_qty = report.kpis["unresolved_venue_quantity"]
    venue_lags = {v.venue_id: v.quote_lag_ticks for v in bundle.config.venues}
    direction_ok = all(venue_lags[e.source_venue] < venue_lags[e.target_venue] for e in graph.lead_lag.edges)
    shuffled = list(reversed(graph.lead_lag.edge_records()))
    deterministic = calculate_influence(build_venue_graph(shuffled, graph.lead_lag.venues)).scores == graph.influence.scores
    latency_case = next(b for b in benchmarks if b.case == "feed_latency_confounding")
    decisions = bundle.decisions
    candidates = sum(len(d.candidate_venues) for d in decisions)
    with_features = sum(1 for d in decisions for v in d.candidate_venues if v in d.decision_features)
    known_models = sum(1 for d in decisions if d.router_model_version in pipeline.router_models)
    reproduced = sum(1 for r in reconstructions if r.status is ReconstructionStatus.REPRODUCED)
    excluded = [x for d in decisions for x in d.excluded_venues]
    missing_d = float(sum(1 for x in bundle.market_data.venue_mids["VENUE_D"] if x != x) / len(bundle.market_data.venue_mids["VENUE_D"]))
    cum_mismatch = len(exc.filter(exception_type=ExceptionType.CUMQTY_MISMATCH))
    overfill = len(exc.filter(exception_type=ExceptionType.OVERFILL))
    return {
        "silent_venue_fallbacks": report.kpis["silent_venue_fallbacks"],
        "silent_order_type_coercions": coercions,
        "duplicate_volume_included": len(report.executions) - len({(e.source_system, e.original_exec_id) for e in report.executions}),
        "supported_mapping_coverage": len(supported_ok) / len(supported) if supported else 1.0,
        "raw_retention": (ingestion.stored + ingestion.duplicate_deliveries) / ingestion.captures,
        "raw_chain_ok": pipeline.raw_store.verify_chain().ok,
        "historical_reproducibility": reproducible,
        "lineage_ok": lineage_exc == lineage_flagged,
        "lineage_text": f"{report.kpis['lineage_complete_ratio']:.4%} complete; {lineage_exc} with explicit exception",
        "unresolved_monitored": monitored,
        "attribution_ok": all(e.source_event_ids[-1] in flagged_events or e.source_event_ids[0] in flagged_events
                              for e in report.executions if e.venue_key == "UNRESOLVED" and e.quantity > 0),
        "attribution_text": f"{report.kpis['actual_venue_attribution_ratio']:.4%} attributed; {unresolved_qty} shares UNRESOLVED (explicit)",
        "reconciliation_ok": True,
        "reconciliation_text": f"{len(report.children)} child orders; {cum_mismatch} CumQty mismatch, {overfill} overfill (explicit exceptions)",
        "tca_coverage": tca.coverage["coverage_ratio"],
        "markout_text": f"incomplete={tca.coverage['incomplete_horizons']} stale={tca.coverage['stale_horizons']}",
        "stale_included": tca.coverage["stale_midpoints_included"],
        "edge_direction_ok": direction_ok,
        "edge_direction_text": f"{len(graph.lead_lag.edges)} edges, all from shorter to longer quote lag (ground truth)"
        if direction_ok else "edge contradicts ground-truth lag order",
        "centrality_deterministic": deterministic,
        "centrality_text": "identical under reversed load order" if deterministic else "order dependent",
        "graph_benchmarks_ok": all(b.passed for b in benchmarks),
        "graph_benchmarks_text": f"{sum(b.passed for b in benchmarks)}/{len(benchmarks)} cases",
        "latency_ok": latency_case.passed,
        "latency_text": latency_case.detail,
        "missing_data_text": f"VENUE_D grid {missing_d:.1%} missing (pairwise-complete); excluded venues: "
                             f"{sorted(graph.lead_lag.excluded_venues) or 'none'}",
        "model_version_lineage": sum(1 for v in venue_intel if v.model_version) / len(venue_intel),
        "candidate_capture": 1.0 if all(d.candidate_venues for d in decisions) else 0.0,
        "feature_capture": with_features / candidates if candidates else 1.0,
        "decision_model_version": known_models / len(decisions),
        "reconstruction_rate": reproduced / len(reconstructions),
        "exclusion_reasons": sum(1 for x in excluded if x.reason) / len(excluded) if excluded else 1.0,
        "shadow_sessions": shadow.sessions,
        "replay_deterministic": replay_check["deterministic"],
        "test_suite_passed": None if test_results.get("status") == "NOT_RUN" else test_results.get("status") == "PASSED",
    }


def _ground_truth_check(bundle, pipeline: AuditPipeline, report: ReconciliationReport) -> dict[str, Any]:
    planted = bundle.ground_truth["planted_faults"]
    exc = pipeline.exceptions
    keys = {(r.exception_type, r.entity_key) for r in exc.records()}
    busted = {e.original_exec_id for e in report.executions if e.status.value == "BUSTED"}
    corrected = {e.original_exec_id for e in report.executions if len(e.chain) > 1 and e.status.value == "ACTIVE"}
    return {
        "possdup_resends_deduplicated": all((ExceptionType.DUPLICATE_EXEC_ID, x) in keys for x in planted["possdup_exec_ids"]),
        "busts_applied": set(planted["busted_exec_ids"]) == busted,
        "corrections_applied": set(planted["corrected_exec_ids"]) == corrected,
        "orphan_detected": all((ExceptionType.ORPHAN_EXECUTION, x) in keys for x in planted["orphan_exec_ids"]),
        "sequence_gap_detected": len(exc.filter(exception_type=ExceptionType.SEQUENCE_GAP)) >= 1,
        "gap_recovered": any(v["recovered"] for v in pipeline.sequence_monitor.summary().values()),
        "clock_drift_flagged": len(exc.filter(exception_type=ExceptionType.CLOCK_DRIFT)) == len(planted["clock_drift_exec_ids"]),
        "missing_order_type_kept_unresolved": len(exc.filter(exception_type=ExceptionType.MISSING_ORDER_TYPE)) == 1,
        "corrupt_message_retained_and_flagged": len(exc.filter(exception_type=ExceptionType.PROTOCOL_VALIDATION_FAILURE)) >= 1,
        "unknown_tags_flagged": len(exc.filter(exception_type=ExceptionType.UNKNOWN_FIX_TAG)) == len(planted["unknown_tag_exec_ids"]),
        "missing_lastmkt_unresolved": len(exc.filter(exception_type=ExceptionType.UNRESOLVED_EXECUTION_VENUE)) == len(planted["missing_lastmkt_exec_ids"]),
        "expired_mic_invalid": len(exc.filter(exception_type=ExceptionType.INVALID_VENUE)) == len(planted["expired_mic_exec_ids"]),
        "lookahead_detected": all((ExceptionType.LOOKAHEAD_FEATURE, f"{d}|VENUE_C") in keys or
                                  any(r.exception_type is ExceptionType.LOOKAHEAD_FEATURE and (r.entity_key or "").startswith(d)
                                      for r in exc.records()) for d in planted["lookahead_decisions"]),
        "stale_weights_detected": all(any(r.exception_type is ExceptionType.DECISION_NOT_REPRODUCIBLE and r.entity_key == d
                                          for r in exc.records()) for d in planted["stale_weight_decisions"]),
    }


def render_markdown(package: dict[str, Any], steps: list[dict[str, Any]], *, tca_table: str) -> str:
    cd = package["classification_defect"]
    loc = cd["localisation"]
    cont = cd["contamination"]
    rem = cd["remediation"]
    sor = package["sor_decision_reconstruction"]
    shadow = sor["shadow_evaluation"]
    lines = [
        "# Venue Integrity Audit — evidence report",
        "",
        f"* Audit execution ID: `{package['audit_execution_id']}`",
        f"* Package digest: `{package['package_digest']}`",
        f"* Mode: **{package['mode']}** (synthetic data; no live order routing)",
        f"* Raw events: {package['raw_event_counts_and_hashes']['stored']} stored, chain head "
        f"`{package['raw_event_counts_and_hashes']['chain_head_hash'][:16]}…` (ok={package['raw_event_counts_and_hashes']['chain_ok']})",
        f"* Reference data: audited v{package['reference_data_snapshot']['audited_version']} → current "
        f"v{package['reference_data_snapshot']['current']['registry_version']}",
        "",
        "## 1. Finding: where the classification changes",
        "",
        loc["conclusion"],
        "",
        "| Divergence stage | Orders |",
        "|---|---:|",
        *[f"| {k} | {v} |" for k, v in loc["divergence_stage_counts"].items()],
        "",
        "Classification ⊥ venue identity (raw OrdType held fixed):",
        "",
        "| Raw OrdType | Venue mapped: n / changed | Venue unmapped: n / changed | Fisher p |",
        "|---|---|---|---|",
        *[f"| {t} | {v['venue_mapped']['n']} / {v['venue_mapped']['label_changed']} | "
          f"{v['venue_unmapped']['n']} / {v['venue_unmapped']['label_changed']} | "
          f"{'—' if v['fisher_exact_p'] is None else f'{v['fisher_exact_p']:.2e}'} |"
          for t, v in loc["classification_venue_independence"].items()],
        "",
        "## 2. Contamination over time",
        "",
        "| Session | Market orders | LEGACY-labelled | Legacy share |",
        "|---|---:|---:|---:|",
        *[f"| {row['label']} | {row['market_orders']} | {row['legacy_market_orders']} | {row['legacy_share']:.3f} |"
          for row in cont["series"]],
        "",
        "Change points: " + ("; ".join(
            f"{c['label']} via {c['method']}" + (f" — nearest preceding change: {c['nearest_preceding_change']['description']}"
                                                if c.get("nearest_preceding_change") else "")
            for c in cont["change_points"]) or "none"),
        "",
        f"LEGACY bucket by corrected venue: {cont['concentration']['by_corrected_venue']} "
        f"(HHI {cont['concentration']['hhi']}).",
        "",
        f"Association with graph influence: Spearman ρ = {cont['influence_association']['spearman_rho']} "
        f"over {cont['influence_association']['n_venues']} venues (low power: {cont['influence_association']['low_power']}). "
        f"_{cont['caveat']}_",
        "",
        "## 3. Remediation",
        "",
        f"* Maker-checker enforced (self-approval rejected): **{rem['maker_checker_enforced']}**",
        f"* Registry versions: {', '.join(rem['registry_versions'])}",
        f"* Exemplar after reprocessing: {rem['exemplar_after']}",
        f"* Raw payload hash unchanged: **{rem['raw_payload_hash_unchanged']}**; hash-chain head unchanged: "
        f"**{rem['raw_chain_head_unchanged']}**",
        f"* Replay under v2.0 reproduces original mappings: **{rem['historical_replay_v2_0_reproducible']:.2%}**",
        f"* Classification changes caused by the registry change: **{rem['classification_changes_due_to_registry_change']}**",
        f"* Exceptions resolved and reprocessed: {rem['exceptions_resolved_and_reprocessed']}",
        "",
        "## 4. Venue TCA (actual execution venue)",
        "",
        tca_table,
        "",
        f"VENUE_C as production analytics saw it (LEGACY bucket excluded): "
        f"{(package['tca_report'].get('production_view_venue_c') or {}).get('fills')} fills, "
        f"AS@{package['tca_report']['reference_horizon_ms']}ms = "
        f"{((package['tca_report'].get('production_view_venue_c') or {}).get('horizons') or {}).get(str(package['tca_report']['reference_horizon_ms']), {}).get('adverse_selection_bps')} bps.",
        "",
        "## 5. Graph model",
        "",
        "Edges (i → j: i leads j): " + ", ".join(f"{e['source']}→{e['target']} (net {e['net_lead']})"
                                               for e in package["graph_model_output"]["lead_lag"]["edges"]),
        "",
        "Influence: " + ", ".join(f"{k} {v}" for k, v in package["graph_model_output"]["influence"]["scores"].items()),
        "",
        "Without feed-latency adjustment: " + ", ".join(package["graph_model_output"]["without_latency_adjustment"]),
        "",
        "| Benchmark | Passed | Detail |",
        "|---|---|---|",
        *[f"| {b['case']} | {b['passed']} | {b['detail']} |" for b in package["graph_model_output"]["benchmarks"]],
        "",
        "## 6. SOR audit",
        "",
        f"Decision reconstruction: {sor['status_counts']}",
        "",
        *[f"* `{r['decision_id']}` {r['status']}: {'; '.join(r['issues']) or 'score/selection differ'}"
          for r in sor["non_reproduced"]],
        "",
        "Router feature integrity (recorded adverse-selection feature vs corrected point-in-time measurement): "
        f"venues flagged {sor['feature_integrity']['venues_flagged']}.",
        "",
        "| Session | Venue | Decisions | Recorded AS (bps) | Measured AS (bps) | Gap | Flagged |",
        "|---:|---|---:|---:|---:|---:|---|",
        *[f"| {r['session']} | {r['venue']} | {r['decisions']} | {r['recorded_mean']} | {r['measured_point_in_time']} | "
          f"{r['gap']} | {r['flagged']} |" for r in sor["feature_integrity"]["rows"] if r["venue"] == "VENUE_C"],
        "",
        f"Shadow evaluation ({shadow['decisions']} decisions, {shadow['sessions']} sessions; {shadow['mode']}):",
        "",
        "| Policy | Mean IS (bps) | Fill rate | AS (bps) | HHI |",
        "|---|---:|---:|---:|---:|",
        *[f"| {name} | {p['mean_shortfall_bps']} | {p['fill_rate']} | {p['mean_adverse_selection_bps']} | "
          f"{p['venue_concentration_hhi']} |" for name, p in shadow["policies"].items()],
        "",
        "| Candidate | ΔIS vs baseline (bps) | 95% CI | Decisions changed |",
        "|---|---:|---|---:|",
        *[f"| {name} | {c['mean_delta_is_bps']} | [{c['ci'][0]}, {c['ci'][1]}] | {c['decisions_with_different_venue']} |"
          for name, c in shadow["comparisons_vs_baseline"].items()],
        "",
        "## 7. KPI scorecard (spec §12)",
        "",
        "| § | KPI | Target | Measured | Status |",
        "|---|---|---|---|---|",
        *[f"| {r['section']} | {r['kpi']} | {r['target']} | {r['measured']} | {r['status']} |" for r in package["kpi_scorecard"]],
        "",
        "## 8. Go-live gates",
        "",
        "| Gate | Name | Status | Basis |",
        "|---|---|---|---|",
        *[f"| {g['gate']} | {g['name']} | {g['status']} | {g['basis']} |" for g in package["go_live_gates"]],
        "",
        "## 9. Unresolved exceptions (remain visible)",
        "",
        "| Type | Severity | Raw value | Count |",
        "|---|---|---|---:|",
        *[f"| {g['exception_type']} | {g['severity']} | {g['raw_value']} | {g['count']} |"
          for g in package["exception_report"]["unresolved_by_type_and_raw_value"]],
        "",
        "## 10. Ground-truth check (simulation only)",
        "",
        *[f"* {k}: {v}" for k, v in package["ground_truth_check"].items()],
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin wrapper
    import argparse

    parser = argparse.ArgumentParser(description="Run the VIA prototype acceptance demonstration")
    parser.add_argument("--out", default="audit_output")
    parser.add_argument("--with-tests", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    run_demo(args.out, verbose=not args.quiet, run_tests=args.with_tests)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
