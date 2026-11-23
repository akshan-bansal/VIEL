"""Contract E — router decision (spec §3.5, §10.3).

The spec's example shows only the selected venue's expected values. A decision
record must also retain the complete candidate set, every exclusion with its
reason and the decision-time features of every candidate, so this contract
stores per-candidate features. The flat ``expected_*`` fields of the spec
example are derivable from ``decision_features[selected_venue]``.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from venue_integrity.schemas.common import StrictModel, UtcDatetime
from venue_integrity.schemas.enums import OrderType, Side


class CandidateFeatures(StrictModel):
    """Point-in-time features the router saw for one candidate venue.

    ``None`` means the feature was missing at decision time; the router's
    fallback, if any, must be recorded in ``RouterDecision.fallbacks_applied``.
    """

    fill_probability: float | None = Field(default=None, ge=0, le=1)
    liquidity: float | None = Field(default=None, ge=0, le=1)
    price_score: float | None = Field(default=None, ge=0, le=1)
    adverse_selection_bps: float | None = None
    market_impact_bps: float | None = None
    transaction_cost_bps: float | None = None
    graph_influence: float | None = Field(default=None, ge=0, le=1)
    feature_timestamp: UtcDatetime
    feature_model_version: str


class ExcludedVenue(StrictModel):
    venue_id: str
    reason: str = Field(min_length=1)


class RouterDecision(StrictModel):
    decision_id: str
    parent_order_id: str
    child_order_id: str | None = None
    decision_timestamp: UtcDatetime
    instrument_id: str
    side: Side
    order_type: OrderType
    quantity: Decimal = Field(gt=0)
    candidate_venues: tuple[str, ...] = Field(min_length=1)
    excluded_venues: tuple[ExcludedVenue, ...] = ()
    decision_features: dict[str, CandidateFeatures]
    selected_venue: str | None
    router_score: float | None = None
    router_model_version: str
    reference_data_version: str | None
    decision_reason: str
    fallbacks_applied: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistency(self) -> RouterDecision:
        if self.selected_venue is not None and self.selected_venue not in self.candidate_venues:
            raise ValueError("selected_venue must be one of candidate_venues")
        excluded = {e.venue_id for e in self.excluded_venues}
        if excluded & set(self.candidate_venues):
            raise ValueError("a venue cannot be both a candidate and excluded")
        return self


class RouterDecisionOutcome(StrictModel):
    """Realised execution linked back to the decision after the fact."""

    decision_id: str
    child_order_id: str | None
    filled_quantity: Decimal
    fill_ratio: float | None
    actual_venues: dict[str, Decimal]
    average_price: Decimal | None
    effective_cost_bps: float | None
