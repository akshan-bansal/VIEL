"""Contract F — venue intelligence (spec §3.6, §9).

Every score carries its definition, measurement window, calculation timestamp,
confidence/data quality, model version and an explanation of missing or
excluded observations. Structural influence and venue risk are separate
quantities: risk does not include graph influence (spec §9.1).
"""

from __future__ import annotations

from pydantic import Field

from venue_integrity.schemas.common import StrictModel, UtcDatetime
from venue_integrity.schemas.enums import MarketRegime

SCORE_DEFINITIONS: dict[str, str] = {
    "graph_influence": "Katz out-reach centrality of the venue in the directed lead-lag graph "
    "(edge i->j: i's lagged residual returns predict j's), normalised so the most influential venue = 1.",
    "lead_lag_score": "Largest net directional partial correlation on any outgoing edge (0 if none).",
    "price_discovery_share": "Venue's share of total outgoing edge weight in the lead-lag graph.",
    "structural_influence": "I = alpha*graph_influence + beta*price_discovery_share + gamma*lead_lag_score.",
    "liquidity_score": "Quantity-weighted fill ratio of child orders whose fills executed on the venue.",
    "adverse_selection_bps": "Quantity-weighted mean AS at the reference horizon, "
    "AS = 2s(M_h - M_0)/M_0 x 1e4 with M_0 the consolidated midpoint at execution.",
    "adverse_selection_score": "adverse_selection_bps / scale, clipped to [0, 1].",
    "market_impact_bps": "Quantity-weighted mean one-way cost s(P - M_0)/M_0 x 1e4 plus AS/2 at the reference horizon.",
    "latency_risk": "p99 acknowledgement latency / scale, clipped to [0, 1].",
    "data_quality_score": "1 - share of events attributed to the venue that raised identity or data-quality exceptions.",
    "venue_risk": "R = w1*AS + w2*MI + w3*T + w4*D (scores in [0,1]; D = 1 - data_quality_score).",
    "confidence": "n/(n + n0) with n the number of TCA-eligible fills, multiplied by data_quality_score.",
}


class VenueIntelligence(StrictModel):
    venue_id: str
    calculation_timestamp: UtcDatetime
    window_start: UtcDatetime
    window_end: UtcDatetime
    graph_influence: float | None = Field(default=None, ge=0, le=1)
    lead_lag_score: float | None = None
    price_discovery_share: float | None = None
    structural_influence: float | None = None
    liquidity_score: float | None = None
    adverse_selection_bps: float | None = None
    adverse_selection_score: float | None = None
    market_impact_bps: float | None = None
    latency_risk: float | None = None
    data_quality_score: float = Field(ge=0, le=1)
    venue_risk: float | None = None
    market_regime: MarketRegime = MarketRegime.UNKNOWN
    observations: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    model_version: str
    exclusions: dict[str, int] = Field(default_factory=dict)
    notes: tuple[str, ...] = ()
