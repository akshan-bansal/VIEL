"""Router feature-integrity audit (spec §10: did the router operate on correct information?).

Compares the features recorded in each SOR decision with the audit's own
point-in-time measurement on corrected venue identity (earlier sessions only).
A persistent gap for a venue is evidence that the router's feature feed was
contaminated — for example by mis-attributed executions — independent of
whether its scoring logic was reproduced correctly.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from venue_integrity.identity.exception_queue import Finding
from venue_integrity.schemas.enums import ExceptionType
from venue_integrity.schemas.router_decision import RouterDecision


def audit_feature_integrity(
    decisions: list[RouterDecision],
    corrected,  # callable: RouterDecision -> dict[venue, CandidateFeatures]
    decision_session: dict[str, int],
    *,
    feature: str = "adverse_selection_bps",
    threshold_bps: float = 1.0,
    min_decisions: int = 10,
) -> tuple[dict[str, Any], list[Finding]]:
    recorded: dict[tuple[int, str], list[float]] = defaultdict(list)
    measured: dict[tuple[int, str], list[float]] = defaultdict(list)
    for d in decisions:
        session = decision_session[d.decision_id]
        if session == 0:
            continue  # no earlier sessions to measure against
        ref = corrected(d)
        for venue, f in d.decision_features.items():
            rv = getattr(f, feature)
            mv = getattr(ref.get(venue), feature, None) if venue in ref else None
            if rv is None or mv is None or ref[venue] is f:
                continue
            recorded[(session, venue)].append(rv)
            measured[(session, venue)].append(mv)
    rows = []
    findings = []
    for key in sorted(recorded):
        session, venue = key
        if len(recorded[key]) < min_decisions:
            continue
        r_mean = float(np.mean(recorded[key]))
        m_mean = float(np.mean(measured[key]))
        gap = r_mean - m_mean
        flagged = abs(gap) > threshold_bps
        rows.append({"session": session, "venue": venue, "decisions": len(recorded[key]),
                     "recorded_mean": round(r_mean, 4), "measured_point_in_time": round(m_mean, 4),
                     "gap": round(gap, 4), "flagged": flagged})
    by_venue: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_venue[row["venue"]].append(row)
    for venue, venue_rows in sorted(by_venue.items()):
        flagged_rows = [r for r in venue_rows if r["flagged"]]
        if flagged_rows:
            first = min(r["session"] for r in flagged_rows)
            findings.append(Finding(
                ExceptionType.ROUTER_FEATURE_DIVERGENCE,
                f"{venue}: recorded router {feature} differs from corrected point-in-time measurement by more than "
                f"{threshold_bps} bps in {len(flagged_rows)} of {len(venue_rows)} sessions (first: session {first}; "
                f"mean gap {np.mean([r['gap'] for r in flagged_rows]):+.2f} bps)",
                entity_key=f"{venue}|{feature}",
                context={"sessions_flagged": [r["session"] for r in flagged_rows]},
            ))
    summary = {
        "feature": feature,
        "threshold_bps": threshold_bps,
        "rows": rows,
        "venues_flagged": sorted({r["venue"] for r in rows if r["flagged"]}),
    }
    return summary, findings
