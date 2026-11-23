"""Classification-contamination detector (spec §9.2).

    L_t = V(LEGACY-labelled market orders)_t / V(market orders)_t

measured on the *audited* pipeline's labels, where "market order" is decided
from the raw protocol value (FIX OrdType) — never from the audited label.

The detector reports:

* sudden increases in L_t (rolling-baseline z-score and one-sided CUSUM),
  annotated with the nearest preceding mapping/deployment change;
* venue concentration inside the LEGACY bucket, attributing each order to the
  venue its destination resolves to under the *corrected* registry;
* the association between per-venue LEGACY share and graph influence
  (Spearman), explicitly labelled as association, not causation;
* divergence between routing destinations and execution venues;
* one VENUE-002 incident per offending raw destination.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import stats

from venue_integrity.identity.exception_queue import Finding
from venue_integrity.schemas.enums import ExceptionType

LEGACY = "LEGACY"
CAUSALITY_CAVEAT = (
    "Association between LEGACY share and venue influence is descriptive. It does not establish that "
    "influence causes mis-classification; the mechanism must be established by stage-level defect localisation."
)


@dataclass(frozen=True)
class ClassifiedOrder:
    event_id: str
    bucket: int
    bucket_label: str
    raw_ord_type: str | None
    audited_label: str
    raw_destination: str | None
    corrected_venue_id: str | None
    quantity: float


@dataclass(frozen=True)
class ChangeEvent:
    bucket: int
    description: str


@dataclass
class ContaminationReport:
    series: list[dict[str, Any]]
    change_points: list[dict[str, Any]]
    concentration: dict[str, Any]
    influence_association: dict[str, Any]
    destination_divergence: list[dict[str, Any]]
    incidents: list[dict[str, Any]]
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "series": self.series,
            "change_points": self.change_points,
            "concentration": self.concentration,
            "influence_association": self.influence_association,
            "destination_divergence": self.destination_divergence,
            "incidents": self.incidents,
            "caveat": CAUSALITY_CAVEAT,
        }


def _hhi(shares: dict[str, float]) -> float:
    return float(sum(s * s for s in shares.values()))


def cusum_upward(values: list[float], k: float, h: float) -> int | None:
    """Index of the first upward CUSUM alarm relative to the first value, or None."""
    if not values:
        return None
    target = values[0]
    s = 0.0
    for i, x in enumerate(values):
        s = max(0.0, s + (x - target - k))
        if s > h:
            return i
    return None


def detect_contamination(
    orders: list[ClassifiedOrder],
    *,
    venue_influence: dict[str, float],
    change_events: list[ChangeEvent],
    destination_pairs: list[tuple[int, str | None, str | None]] | None = None,
    baseline_window: int = 5,
    zscore_threshold: float = 4.0,
    cusum_k: float = 0.05,
    cusum_h: float = 0.25,
) -> ContaminationReport:
    market = [o for o in orders if o.raw_ord_type == "1"]
    buckets = sorted({o.bucket for o in orders})
    labels = {o.bucket: o.bucket_label for o in orders}

    series = []
    for b in buckets:
        in_bucket = [o for o in market if o.bucket == b]
        vol = sum(o.quantity for o in in_bucket)
        legacy_vol = sum(o.quantity for o in in_bucket if o.audited_label == LEGACY)
        series.append({
            "bucket": b,
            "label": labels[b],
            "market_volume": vol,
            "legacy_market_volume": legacy_vol,
            "legacy_share": legacy_vol / vol if vol else 0.0,
            "market_orders": len(in_bucket),
            "legacy_market_orders": sum(1 for o in in_bucket if o.audited_label == LEGACY),
        })

    shares = [row["legacy_share"] for row in series]
    change_points = []
    for i in range(baseline_window, len(shares)):
        window = np.array(shares[i - baseline_window:i])
        mu, sd = float(window.mean()), float(window.std(ddof=1)) if baseline_window > 1 else 0.0
        jump = shares[i] - mu
        z = jump / sd if sd > 1e-12 else (np.inf if jump > 1e-12 else 0.0)
        if z > zscore_threshold:
            prior = [c for c in change_events if c.bucket <= series[i]["bucket"]]
            nearest = max(prior, key=lambda c: c.bucket) if prior else None
            change_points.append({
                "bucket": series[i]["bucket"],
                "label": series[i]["label"],
                "method": "rolling_zscore",
                "legacy_share": round(shares[i], 6),
                "baseline_mean": round(mu, 6),
                "zscore": None if not np.isfinite(z) else round(float(z), 3),
                "nearest_preceding_change": None if nearest is None else
                {"bucket": nearest.bucket, "label": labels.get(nearest.bucket), "description": nearest.description},
            })
    alarm = cusum_upward(shares, cusum_k, cusum_h)
    if alarm is not None:
        change_points.append({"bucket": series[alarm]["bucket"], "label": series[alarm]["label"], "method": "cusum",
                              "legacy_share": round(shares[alarm], 6)})

    # concentration inside the LEGACY bucket by corrected venue
    legacy_orders = [o for o in market if o.audited_label == LEGACY]
    by_venue: dict[str, float] = defaultdict(float)
    for o in legacy_orders:
        by_venue[o.corrected_venue_id or "UNRESOLVED"] += o.quantity
    total_legacy = sum(by_venue.values())
    venue_shares = {k: v / total_legacy for k, v in sorted(by_venue.items())} if total_legacy else {}
    concentration = {
        "legacy_market_volume": total_legacy,
        "by_corrected_venue": {k: round(v, 6) for k, v in venue_shares.items()},
        "hhi": round(_hhi(venue_shares), 6) if venue_shares else None,
        "top_venue": max(venue_shares, key=venue_shares.get) if venue_shares else None,
    }

    # per-venue LEGACY share of market volume vs influence
    vol_by_venue: dict[str, float] = defaultdict(float)
    legacy_by_venue: dict[str, float] = defaultdict(float)
    for o in market:
        if o.corrected_venue_id is None:
            continue
        vol_by_venue[o.corrected_venue_id] += o.quantity
        if o.audited_label == LEGACY:
            legacy_by_venue[o.corrected_venue_id] += o.quantity
    rows = []
    for venue in sorted(vol_by_venue):
        rows.append({"venue": venue, "legacy_share": legacy_by_venue[venue] / vol_by_venue[venue],
                     "graph_influence": venue_influence.get(venue)})
    paired = [(r["legacy_share"], r["graph_influence"]) for r in rows if r["graph_influence"] is not None]
    rho = p_value = None
    if len(paired) >= 3 and len({a for a, _ in paired}) > 1 and len({b for _, b in paired}) > 1:
        res = stats.spearmanr([a for a, _ in paired], [b for _, b in paired])
        rho, p_value = float(res.statistic), float(res.pvalue)
    association = {
        "per_venue": [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()} for r in rows],
        "spearman_rho": None if rho is None else round(rho, 6),
        "p_value": None if p_value is None else round(p_value, 6),
        "n_venues": len(paired),
        "low_power": len(paired) < 8,
        "caveat": CAUSALITY_CAVEAT,
    }

    divergence = []
    if destination_pairs:
        per_bucket: dict[int, list[tuple[str | None, str | None]]] = defaultdict(list)
        for b, intended, actual in destination_pairs:
            per_bucket[b].append((intended, actual))
        for b in sorted(per_bucket):
            pairs = per_bucket[b]
            comparable = [(i, a) for i, a in pairs if i is not None and a is not None]
            divergence.append({
                "bucket": b,
                "label": labels.get(b),
                "fills": len(pairs),
                "comparable": len(comparable),
                "mismatch_rate": (sum(1 for i, a in comparable if i != a) / len(comparable)) if comparable else None,
                "unresolved_side": len(pairs) - len(comparable),
            })

    incidents = []
    findings = []
    by_destination: dict[str | None, list[ClassifiedOrder]] = defaultdict(list)
    for o in legacy_orders:
        by_destination[o.raw_destination].append(o)
    for destination in sorted(by_destination, key=lambda d: d or ""):
        group = by_destination[destination]
        incident = {
            "raw_destination": destination,
            "market_orders_labelled_legacy": len(group),
            "quantity": sum(o.quantity for o in group),
            "corrected_venues": sorted({o.corrected_venue_id or "UNRESOLVED" for o in group}),
            "first_bucket": labels[min(o.bucket for o in group)],
            "sample_event_ids": sorted(o.event_id for o in group)[:20],
        }
        incidents.append(incident)
        findings.append(Finding(
            ExceptionType.MARKET_ORDER_LEGACY_CLASSIFICATION,
            f"{len(group)} market orders routed to {destination!r} were labelled LEGACY by the audited pipeline",
            entity_key=f"destination|{destination}",
            raw_value=destination,
            context={k: v for k, v in incident.items() if k != "sample_event_ids"},
        ))
    return ContaminationReport(series, change_points, concentration, association, divergence, incidents, findings)
