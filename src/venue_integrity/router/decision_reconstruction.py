"""Independent reconstruction of recorded SOR decisions (spec §10.2–10.3).

Re-scores every recorded decision with the exact router model version it was
made with and checks that:

* the model version and reference-data version are recorded and known;
* every candidate venue is a known venue identity in that reference-data version;
* every candidate has a complete, point-in-time feature set (no feature stamped
  after the decision — look-ahead);
* missing features are covered by a recorded fallback;
* the recomputed utility and the selected venue match the record.

The reconstruction never re-routes anything: it is evidence about whether the
router operated on correct information.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from venue_integrity.execution.execution_reconciler import ReconciliationReport
from venue_integrity.identity.exception_queue import Finding
from venue_integrity.identity.mic_registry import VenueRegistry
from venue_integrity.router.venue_scoring import RouterModel, missing_features, score_candidates, select_venue
from venue_integrity.schemas.enums import ExceptionType
from venue_integrity.schemas.router_decision import RouterDecision, RouterDecisionOutcome
from venue_integrity.tca.engine import TcaReport


class ReconstructionStatus(StrEnum):
    REPRODUCED = "REPRODUCED"
    SCORE_MISMATCH = "SCORE_MISMATCH"
    SELECTION_MISMATCH = "SELECTION_MISMATCH"
    NOT_REPRODUCIBLE = "NOT_REPRODUCIBLE"


@dataclass
class ReconstructionResult:
    decision_id: str
    status: ReconstructionStatus
    recorded_selection: str | None
    recomputed_selection: str | None
    recorded_score: float | None
    recomputed_scores: dict[str, float | None]
    issues: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    outcome: RouterDecisionOutcome | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "status": self.status.value,
            "recorded_selection": self.recorded_selection,
            "recomputed_selection": self.recomputed_selection,
            "recorded_score": self.recorded_score,
            "recomputed_scores": {k: (round(v, 10) if v is not None else None) for k, v in self.recomputed_scores.items()},
            "issues": self.issues,
            "outcome": self.outcome.model_dump(mode="json") if self.outcome else None,
        }


def link_outcome(decision: RouterDecision, reconciliation: ReconciliationReport | None,
                 tca: TcaReport | None) -> RouterDecisionOutcome | None:
    if reconciliation is None or decision.child_order_id is None:
        return None
    child = next((c for c in reconciliation.children if c.child_order_id == decision.child_order_id), None)
    if child is None:
        return None
    execs = [e for e in reconciliation.executions
             if e.child_order_id == decision.child_order_id and e.quantity > 0]
    qty = sum((e.quantity for e in execs), Decimal(0))
    avg = (sum((e.quantity * e.price for e in execs), Decimal(0)) / qty) if qty > 0 else None
    cost = None
    if tca is not None:
        fills = [f for f in tca.fills if f.child_order_id == decision.child_order_id and f.included]
        total = sum(f.quantity for f in fills)
        if total > 0:
            cost = sum(f.effective_cost_bps * f.quantity for f in fills) / total
    return RouterDecisionOutcome(
        decision_id=decision.decision_id,
        child_order_id=decision.child_order_id,
        filled_quantity=qty,
        fill_ratio=child.fill_ratio,
        actual_venues={k: v for k, v in sorted(child.venue_breakdown.items())},
        average_price=avg.quantize(Decimal("0.000001")) if avg is not None else None,
        effective_cost_bps=cost,
    )


def reconstruct_decision(
    decision: RouterDecision,
    models: dict[str, RouterModel],
    *,
    registry: VenueRegistry,
    tolerance: float = 1e-9,
    reconciliation: ReconciliationReport | None = None,
    tca: TcaReport | None = None,
) -> ReconstructionResult:
    issues: list[str] = []
    findings: list[Finding] = []
    did = decision.decision_id
    not_reproducible = False

    model = models.get(decision.router_model_version)
    if model is None:
        issues.append(f"router model version {decision.router_model_version} unknown")
        not_reproducible = True

    if decision.reference_data_version is None:
        issues.append("reference-data version not recorded")
        not_reproducible = True
        known_venues: set[str] = set(registry.venue_ids())
    elif not registry.has_version(decision.reference_data_version):
        issues.append(f"reference-data version {decision.reference_data_version} unknown")
        not_reproducible = True
        known_venues = set(registry.venue_ids())
    else:
        known_venues = set(registry.venue_ids(decision.reference_data_version))
    for venue in decision.candidate_venues:
        if venue not in known_venues:
            issues.append(f"candidate {venue} is not a venue identity in reference data")

    for venue in decision.candidate_venues:
        features = decision.decision_features.get(venue)
        if features is None:
            issues.append(f"no features recorded for candidate {venue}")
            findings.append(Finding(ExceptionType.MISSING_MODEL_FEATURE, f"decision {did}: candidate {venue} has no features",
                                    entity_key=f"{did}|{venue}"))
            not_reproducible = True
            continue
        missing = missing_features(features)
        if missing:
            covered = any(venue in fb for fb in decision.fallbacks_applied)
            issues.append(f"{venue} missing {missing}" + (" (fallback recorded)" if covered else " (no fallback recorded)"))
            findings.append(Finding(ExceptionType.MISSING_MODEL_FEATURE,
                                    f"decision {did}: {venue} missing {', '.join(missing)}",
                                    entity_key=f"{did}|{venue}", context={"fallback_recorded": covered}))
            not_reproducible = True  # the score the router used for this venue cannot be recomputed
        if features.graph_influence is None:
            issues.append(f"{venue} graph_influence missing")
        if features.feature_timestamp > decision.decision_timestamp:
            issues.append(f"{venue} features stamped after the decision (look-ahead)")
            findings.append(Finding(ExceptionType.LOOKAHEAD_FEATURE,
                                    f"decision {did}: {venue} features at {features.feature_timestamp.isoformat()} "
                                    f"> decision {decision.decision_timestamp.isoformat()}",
                                    entity_key=f"{did}|{venue}"))
            not_reproducible = True

    for fallback in decision.fallbacks_applied:
        issues.append(f"approved fallback applied: {fallback}")
        findings.append(Finding(ExceptionType.MISSING_MODEL_FEATURE, f"decision {did}: fallback {fallback}",
                                entity_key=f"{did}|{fallback.split(':', 1)[0]}", context={"fallback_recorded": True}))

    scores: dict[str, float | None] = {}
    selection = None
    if model is not None:
        scores = score_candidates({v: decision.decision_features[v] for v in decision.candidate_venues
                                   if v in decision.decision_features}, model)
        selection = select_venue(scores, model)

    if not_reproducible:
        status = ReconstructionStatus.NOT_REPRODUCIBLE
    elif selection != decision.selected_venue:
        status = ReconstructionStatus.SELECTION_MISMATCH
    elif (decision.router_score is not None and selection is not None
          and abs((scores.get(selection) or 0.0) - decision.router_score) > tolerance):
        status = ReconstructionStatus.SCORE_MISMATCH
    else:
        status = ReconstructionStatus.REPRODUCED
    if status is not ReconstructionStatus.REPRODUCED:
        findings.append(Finding(ExceptionType.DECISION_NOT_REPRODUCIBLE,
                                f"decision {did}: {status.value}" + (f" ({'; '.join(issues)})" if issues else ""),
                                entity_key=did, context={"recorded": decision.selected_venue, "recomputed": selection}))
    return ReconstructionResult(
        decision_id=did,
        status=status,
        recorded_selection=decision.selected_venue,
        recomputed_selection=selection,
        recorded_score=decision.router_score,
        recomputed_scores=scores,
        issues=issues,
        findings=findings,
        outcome=link_outcome(decision, reconciliation, tca),
    )
