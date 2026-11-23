"""Shadow-routing evaluation (spec §10.4).

Three analytical policies are evaluated on the *same* historical decisions:

* **baseline**             — the production logic with the features it actually saw;
* **integrity_corrected**  — the same logic with venue features recomputed on
                             corrected venue identity (point-in-time);
* **graph_enhanced**       — corrected identity plus regime-conditional adverse
                             selection informed by the validated lead-lag graph.

Policies only *recommend* a venue. Nothing is sent anywhere.

Counterfactual outcomes for venues that were not chosen are unobservable in
replay, so an :class:`OutcomeModel` must be supplied. In simulation mode it is
the simulator's ground truth; for production data it must be an estimated,
independently validated cost model. Outcomes use common random numbers keyed
by (decision, venue), so policies are compared on matched orders with
identical market snapshots. Differences are paired, bootstrapped and
stratified by regime and order-size bucket; a lower mean alone does not
establish an improvement.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from venue_integrity.router.venue_scoring import RouterModel, score_candidates, select_venue
from venue_integrity.schemas.router_decision import CandidateFeatures, RouterDecision


@dataclass(frozen=True)
class SimOutcome:
    fill_ratio: float
    shortfall_bps: float  # child-level IS incl. fees and opportunity cost
    effective_spread_bps: float
    realized_spread_bps: float
    adverse_selection_bps: float
    impact_bps: float
    opportunity_cost_bps: float
    latency_ms: float


class OutcomeModel(Protocol):
    def outcome(self, decision: RouterDecision, venue_id: str, regime: str) -> SimOutcome: ...


FeatureProvider = Callable[[RouterDecision], dict[str, CandidateFeatures]]


@dataclass
class Policy:
    name: str
    model: RouterModel
    features: FeatureProvider


@dataclass
class PolicyResult:
    name: str
    selections: dict[str, str | None]
    outcomes: dict[str, SimOutcome]


@dataclass
class ShadowReport:
    policies: dict[str, dict[str, Any]]
    comparisons: dict[str, dict[str, Any]]
    sessions: int
    decisions: int
    min_sessions: int
    notes: list[str] = field(default_factory=list)

    @property
    def meets_minimum_window(self) -> bool:
        return self.sessions >= self.min_sessions

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "SHADOW_ONLY — recommendations are not routed; production control requires approval (Gate 5)",
            "decisions": self.decisions,
            "sessions": self.sessions,
            "min_sessions": self.min_sessions,
            "meets_minimum_window": self.meets_minimum_window,
            "policies": self.policies,
            "comparisons_vs_baseline": self.comparisons,
            "notes": self.notes,
        }


def _bootstrap_ci(diffs: np.ndarray, samples: int, level: float, rng: np.random.Generator) -> tuple[float, float]:
    if len(diffs) < 2:
        return (float("nan"), float("nan"))
    idx = rng.integers(0, len(diffs), size=(samples, len(diffs)))
    means = diffs[idx].mean(axis=1)
    lo, hi = np.percentile(means, [(1 - level) / 2 * 100, (1 + level) / 2 * 100])
    return float(lo), float(hi)


def _hhi(selections: list[str | None]) -> float:
    picked = [s for s in selections if s is not None]
    if not picked:
        return 0.0
    counts = defaultdict(int)
    for s in picked:
        counts[s] += 1
    n = len(picked)
    return float(sum((c / n) ** 2 for c in counts.values()))


def _summary(decisions: list[RouterDecision], result: PolicyResult) -> dict[str, Any]:
    outs = [result.outcomes[d.decision_id] for d in decisions if d.decision_id in result.outcomes]
    if not outs:
        return {"decisions_scored": 0}
    arr = lambda attr: np.array([getattr(o, attr) for o in outs])  # noqa: E731
    venues = defaultdict(int)
    for d in decisions:
        venues[result.selections.get(d.decision_id) or "NONE"] += 1
    return {
        "decisions_scored": len(outs),
        "mean_shortfall_bps": round(float(arr("shortfall_bps").mean()), 6),
        "fill_rate": round(float(arr("fill_ratio").mean()), 6),
        "mean_effective_spread_bps": round(float(arr("effective_spread_bps").mean()), 6),
        "mean_realized_spread_bps": round(float(arr("realized_spread_bps").mean()), 6),
        "mean_adverse_selection_bps": round(float(arr("adverse_selection_bps").mean()), 6),
        "mean_impact_bps": round(float(arr("impact_bps").mean()), 6),
        "mean_opportunity_cost_bps": round(float(arr("opportunity_cost_bps").mean()), 6),
        "p99_latency_ms": round(float(np.percentile(arr("latency_ms"), 99)), 6),
        "venue_concentration_hhi": round(_hhi(list(result.selections.values())), 6),
        "selections": dict(sorted(venues.items())),
    }


def evaluate_shadow(
    decisions: list[RouterDecision],
    policies: list[Policy],
    outcome_model: OutcomeModel,
    *,
    regime_of: Callable[[RouterDecision], str],
    session_of: Callable[[RouterDecision], int],
    size_bucket_of: Callable[[RouterDecision], str],
    min_sessions: int = 20,
    bootstrap_samples: int = 2000,
    confidence_level: float = 0.95,
    seed: int = 20261123,
) -> ShadowReport:
    if not policies or policies[0].name != "baseline":
        raise ValueError("the first policy must be the baseline")
    decisions = sorted(decisions, key=lambda d: (d.decision_timestamp, d.decision_id))
    results: dict[str, PolicyResult] = {}
    for policy in policies:
        selections: dict[str, str | None] = {}
        outcomes: dict[str, SimOutcome] = {}
        for d in decisions:
            features = policy.features(d)
            eligible = {v: f for v, f in features.items() if v in d.candidate_venues}
            choice = select_venue(score_candidates(eligible, policy.model), policy.model)
            selections[d.decision_id] = choice
            if choice is not None:
                outcomes[d.decision_id] = outcome_model.outcome(d, choice, regime_of(d))
        results[policy.name] = PolicyResult(policy.name, selections, outcomes)

    rng = np.random.default_rng(seed)
    base = results["baseline"]
    comparisons: dict[str, dict[str, Any]] = {}
    for policy in policies[1:]:
        cand = results[policy.name]
        matched = [d for d in decisions if d.decision_id in base.outcomes and d.decision_id in cand.outcomes]
        diffs = np.array([cand.outcomes[d.decision_id].shortfall_bps - base.outcomes[d.decision_id].shortfall_bps
                          for d in matched])
        lo, hi = _bootstrap_ci(diffs, bootstrap_samples, confidence_level, rng)
        strata: dict[str, dict[str, Any]] = {}
        for label, key in (("regime", regime_of), ("size_bucket", size_bucket_of)):
            groups: dict[str, list[float]] = defaultdict(list)
            for d, diff in zip(matched, diffs, strict=True):
                groups[key(d)].append(diff)
            strata[label] = {}
            for g, values in sorted(groups.items()):
                values_arr = np.array(values)
                glo, ghi = _bootstrap_ci(values_arr, bootstrap_samples, confidence_level, rng)
                strata[label][g] = {"n": len(values), "mean_delta_is_bps": round(float(values_arr.mean()), 6),
                                    "ci": [round(glo, 6), round(ghi, 6)]}
        changed = sum(1 for d in matched if cand.selections[d.decision_id] != base.selections[d.decision_id])
        comparisons[policy.name] = {
            "matched_decisions": len(matched),
            "decisions_with_different_venue": changed,
            "mean_delta_is_bps": round(float(diffs.mean()), 6) if len(diffs) else None,
            "ci": [round(lo, 6), round(hi, 6)],
            "confidence_level": confidence_level,
            "significant_improvement": bool(len(diffs) > 1 and hi < 0),
            "strata": strata,
        }
    sessions = len({session_of(d) for d in decisions})
    notes = [
        "Delta IS = IS(candidate) - IS(baseline) per matched decision; negative is an improvement.",
        "Counterfactual outcomes come from the supplied outcome model (common random numbers per decision/venue).",
    ]
    if sessions < min_sessions:
        notes.append(f"Only {sessions} sessions evaluated; minimum validation window is {min_sessions}.")
    return ShadowReport(
        policies={name: _summary(decisions, r) for name, r in results.items()},
        comparisons=comparisons,
        sessions=sessions,
        decisions=len(decisions),
        min_sessions=min_sessions,
        notes=notes,
    )
