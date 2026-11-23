"""Venue intelligence and venue risk (spec §3.6, §9.1).

    I_v = alpha * C_v + beta * P_v + gamma * LL_v       structural influence (explanatory)
    R_v = w1 * AS_v + w2 * MI_v + w3 * T_v + w4 * D_v   venue risk

Graph influence is deliberately *absent* from R_v: a venue is not penalised
for being central (spec §7.3, §18). Influence is reported next to risk so that
analysts — and later a validated model — can study the relationship.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import networkx as nx
import numpy as np

from venue_integrity.graph.centrality import InfluenceResult, price_discovery_share
from venue_integrity.graph.lead_lag import LeadLagResult
from venue_integrity.identity.exception_queue import Finding
from venue_integrity.schemas.enums import ExceptionType, MarketRegime
from venue_integrity.schemas.venue_intelligence import VenueIntelligence
from venue_integrity.tca.engine import TcaReport


def _clip01(x: float | None) -> float | None:
    if x is None or not np.isfinite(x):
        return None
    return float(min(1.0, max(0.0, x)))


def venue_risk_score(components: dict[str, float | None], weights: dict[str, float]) -> float | None:
    """R = sum w_k * component_k over adverse_selection, market_impact, latency, data_quality.

    Returns None if any component is missing (never imputes zero)."""
    keys = ("adverse_selection", "market_impact", "latency", "data_quality")
    if any(components.get(k) is None for k in keys):
        return None
    return float(sum(weights[k] * components[k] for k in keys))


def structural_influence(c: float | None, p: float | None, ll: float | None, weights: dict[str, float]) -> float | None:
    if c is None or p is None or ll is None:
        return None
    return float(weights["alpha"] * c + weights["beta"] * p + weights["gamma"] * ll)


def compute_venue_intelligence(
    venues: list[str],
    *,
    tca: TcaReport,
    influence: InfluenceResult | None,
    lead_lag: LeadLagResult | None,
    graph: nx.DiGraph | None,
    ack_latency_ms: dict[str, list[float]],
    data_quality: dict[str, float],
    regime: MarketRegime,
    window: tuple[datetime, datetime],
    calculated_at: datetime,
    model: dict[str, Any],
    model_version: str,
) -> tuple[list[VenueIntelligence], list[Finding]]:
    infl_w = model["influence"]
    risk_w = model["risk"]
    scales = model["scales"]
    prior = float(model["confidence_prior_fills"])
    discovery = price_discovery_share(graph) if graph is not None else {}
    out: list[VenueIntelligence] = []
    findings: list[Finding] = []
    for venue in sorted(venues):
        notes: list[str] = []
        c = influence.scores.get(venue) if influence is not None else None
        if c is None:
            reason = (lead_lag.excluded_venues.get(venue) if lead_lag is not None else None) or "NOT_IN_GRAPH"
            notes.append(f"graph influence unavailable: {reason}")
        p = discovery.get(venue) if graph is not None and venue in graph else None
        ll = None
        if lead_lag is not None and venue in lead_lag.venues:
            outgoing = [e.net_lead for e in lead_lag.edges if e.source_venue == venue]
            ll = max(outgoing) if outgoing else 0.0
        vt = tca.venues.get(venue)
        as_bps = vt.as_bps(tca.reference_horizon_ms) if vt else None
        mi_bps = vt.impact_beyond_touch_bps if vt else None
        n = vt.fills if vt else 0
        if vt is None or n == 0:
            notes.append("no TCA-eligible fills attributed to this venue")
        lat = ack_latency_ms.get(venue) or []
        p99 = float(np.percentile(lat, 99)) if lat else None
        if p99 is None:
            notes.append("no latency observations")
        dq = float(data_quality.get(venue, 1.0))
        as_score = _clip01(as_bps / scales["adverse_selection_bps"]) if as_bps is not None else None
        mi_score = _clip01(mi_bps / scales["market_impact_bps"]) if mi_bps is not None else None
        lat_score = _clip01(p99 / scales["latency_ms"]) if p99 is not None else None
        risk = venue_risk_score(
            {"adverse_selection": as_score, "market_impact": mi_score, "latency": lat_score, "data_quality": 1.0 - dq},
            risk_w,
        )
        if risk is None:
            notes.append("venue_risk not computed: at least one component missing")
        exclusions: dict[str, int] = {}
        if vt is not None:
            exclusions.update(vt.excluded)
            ref = vt.horizons.get(tca.reference_horizon_ms, {})
            if ref.get("incomplete"):
                exclusions[f"INCOMPLETE_{tca.reference_horizon_ms}MS_MARKOUT"] = ref["incomplete"]
            if ref.get("stale"):
                exclusions[f"STALE_{tca.reference_horizon_ms}MS_MARKOUT"] = ref["stale"]
        out.append(
            VenueIntelligence(
                venue_id=venue,
                calculation_timestamp=calculated_at,
                window_start=window[0],
                window_end=window[1],
                graph_influence=c,
                lead_lag_score=ll,
                price_discovery_share=p,
                structural_influence=structural_influence(c, p, ll, infl_w),
                liquidity_score=tca.intended_venue_fill_ratio.get(venue),
                adverse_selection_bps=as_bps,
                adverse_selection_score=as_score,
                market_impact_bps=mi_bps,
                latency_risk=lat_score,
                data_quality_score=dq,
                venue_risk=risk,
                market_regime=regime,
                observations=n,
                confidence=float(n / (n + prior) * dq) if n else 0.0,
                model_version=model_version,
                exclusions=exclusions,
                notes=tuple(notes),
            )
        )
        if c is not None and as_score is not None:
            if (c >= 0.8 and as_score <= 0.2) or (c <= 0.2 and as_score >= 0.8):
                findings.append(Finding(ExceptionType.GRAPH_TCA_DIVERGENCE,
                                        f"{venue}: graph influence {c:.2f} vs adverse-selection score {as_score:.2f}",
                                        entity_key=venue, context={"graph_influence": c, "as_score": as_score}))
    return out, findings
