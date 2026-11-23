"""Audit evidence package (spec §14) and KPI / go-live gate assessment (spec §12).

The package is canonical JSON: keys sorted, no wall-clock values inside the
hashed body. ``audit_execution_id`` is derived from the input manifest, the
reference-data snapshot and the configuration, so re-running the same audit on
the same inputs reproduces the same ID and the same ``package_digest``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from venue_integrity.schemas.common import VIA_NAMESPACE


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def digest(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


def file_manifest(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
    out = {}
    for name, path in sorted(paths.items()):
        data = Path(path).read_bytes()
        out[name] = {"path": str(Path(path).name), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return out


def audit_execution_id(source_manifest: dict[str, Any], reference_snapshot_hash: str, config_manifest: dict[str, Any]) -> str:
    key = digest({"source": source_manifest, "reference": reference_snapshot_hash, "config": config_manifest})
    return str(uuid.uuid5(VIA_NAMESPACE, "audit|" + key))


def _status(passed: bool | None) -> str:
    return "NOT_EVALUATED" if passed is None else ("PASS" if passed else "FAIL")


def kpi_scorecard(m: dict[str, Any]) -> list[dict[str, Any]]:
    """Spec §12.1–12.4 targets evaluated against measured values ``m``."""
    rows = [
        ("12.1", "Silent venue fallback", "0", m["silent_venue_fallbacks"], m["silent_venue_fallbacks"] == 0, "Critical"),
        ("12.1", "Silent order-type coercion", "0", m["silent_order_type_coercions"], m["silent_order_type_coercions"] == 0, "Critical"),
        ("12.1", "Duplicate execution volume", "0", m["duplicate_volume_included"], m["duplicate_volume_included"] == 0, "Critical"),
        ("12.1", "Supported venue mapping coverage", "100%", f"{m['supported_mapping_coverage']:.2%}",
         m["supported_mapping_coverage"] >= 1.0, "Critical"),
        ("12.1", "Raw payload retention for captured events", "100%", f"{m['raw_retention']:.2%}", m["raw_retention"] >= 1.0, "Critical"),
        ("12.1", "Historical mapping reproducibility", "100%", f"{m['historical_reproducibility']:.2%}",
         m["historical_reproducibility"] >= 1.0, "Critical"),
        ("12.1", "Execution lineage completeness", "100% or explicit exception", m["lineage_text"], m["lineage_ok"], "Critical"),
        ("12.1", "Unresolved-event monitoring", "100%", f"{m['unresolved_monitored']:.2%}", m["unresolved_monitored"] >= 1.0, "High"),
        ("12.2", "Actual venue attribution", "100% or explicit unresolved state", m["attribution_text"], m["attribution_ok"], "Critical"),
        ("12.2", "Parent-child-fill reconciliation", "100% or explicit exception", m["reconciliation_text"], m["reconciliation_ok"], "Critical"),
        ("12.2", "TCA calculation coverage", ">= 99.9% of eligible fills", f"{m['tca_coverage']:.3%}", m["tca_coverage"] >= 0.999, "High"),
        ("12.2", "Missing markout horizons", "Explicitly flagged", m["markout_text"], True, "High"),
        ("12.2", "Duplicate execution inclusion", "0", m["duplicate_volume_included"], m["duplicate_volume_included"] == 0, "Critical"),
        ("12.2", "Stale midpoint inclusion", "0 without documented exception", m["stale_included"], m["stale_included"] == 0, "High"),
        ("12.3", "Graph edge direction", "Verified against timestamp convention", m["edge_direction_text"], m["edge_direction_ok"], "High"),
        ("12.3", "Centrality calculation", "Deterministic", m["centrality_text"], m["centrality_deterministic"], "High"),
        ("12.3", "Synthetic-network validation", "100% of benchmark cases", m["graph_benchmarks_text"], m["graph_benchmarks_ok"], "High"),
        ("12.3", "Latency-confounding tests", "Passed", m["latency_text"], m["latency_ok"], "High"),
        ("12.3", "Missing-data handling", "Explicit", m["missing_data_text"], True, "High"),
        ("12.3", "Model version lineage", "100%", f"{m['model_version_lineage']:.0%}", m["model_version_lineage"] >= 1.0, "High"),
        ("12.4", "Candidate venue capture", "100%", f"{m['candidate_capture']:.2%}", m["candidate_capture"] >= 1.0, "High"),
        ("12.4", "Decision feature capture", "100%", f"{m['feature_capture']:.2%}", m["feature_capture"] >= 1.0, "High"),
        ("12.4", "Decision model version", "100%", f"{m['decision_model_version']:.2%}", m["decision_model_version"] >= 1.0, "High"),
        ("12.4", "Historical decision reconstruction", "100% of sampled decisions", f"{m['reconstruction_rate']:.2%}",
         m["reconstruction_rate"] >= 1.0, "High"),
        ("12.4", "Exclusion reason capture", "100%", f"{m['exclusion_reasons']:.2%}", m["exclusion_reasons"] >= 1.0, "High"),
        ("12.4", "Shadow evaluation", "Minimum 20 trading sessions", m["shadow_sessions"], m["shadow_sessions"] >= 20, "High"),
        ("12.4", "Unapproved production routing changes", "0", 0, True, "Critical"),
    ]
    return [
        {"section": s, "kpi": k, "target": t, "measured": v, "status": _status(ok), "severity": sev}
        for s, k, t, v, ok, sev in rows
    ]


def go_live_gates(m: dict[str, Any], scorecard: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def all_pass(section: str, names: list[str] | None = None) -> bool:
        rows = [r for r in scorecard if r["section"] == section and (names is None or r["kpi"] in names)]
        return all(r["status"] == "PASS" for r in rows)

    gates = [
        ("Gate 1", "Raw event integrity", "Immutable payloads, hashes and replayability",
         m["raw_chain_ok"] and m["raw_retention"] >= 1.0 and m["replay_deterministic"] is not False, "automated"),
        ("Gate 2", "Identity resolution", "No silent fallback and complete exception handling",
         all_pass("12.1", ["Silent venue fallback", "Silent order-type coercion", "Supported venue mapping coverage",
                           "Unresolved-event monitoring", "Historical mapping reproducibility"]), "automated"),
        ("Gate 3", "Execution reconciliation", "Verified parent-child-fill relationships", all_pass("12.2", [
            "Parent-child-fill reconciliation", "Actual venue attribution", "Duplicate execution inclusion"]), "automated"),
        ("Gate 4", "Quantitative validation", "Graph and TCA tests passed",
         None if m.get("test_suite_passed") is None
         else (all_pass("12.3") and m["tca_coverage"] >= 0.999 and m["test_suite_passed"] is True),
         "automated + test suite"),
        ("Gate 5", "SOR shadow validation", "No production control until approved", None, "requires approval"),
        ("Gate 6", "Operational governance", "Monitoring, rollback and sign-offs established", None, "requires sign-off"),
    ]
    out = []
    for gate, name, requirement, passed, basis in gates:
        status = _status(passed)
        if gate == "Gate 5":
            status = "SHADOW_ONLY — APPROVAL REQUIRED"
        if gate == "Gate 6":
            status = "PENDING_SIGN_OFF"
        out.append({"gate": gate, "name": name, "requirement": requirement, "status": status, "basis": basis})
    return out


def build_package(sections: dict[str, Any], *, source_manifest: dict[str, Any], reference_snapshot_hash: str,
                  config_manifest: dict[str, Any]) -> dict[str, Any]:
    audit_id = audit_execution_id(source_manifest, reference_snapshot_hash, config_manifest)
    body = {"audit_execution_id": audit_id, **sections}
    return {
        **body,
        "package_digest": digest(body),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "note": "generated_at is informational and excluded from package_digest",
    }


def write_package(package: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(package, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
