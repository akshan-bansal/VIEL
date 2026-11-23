"""Contract C — normalised order event (spec §3.3).

``order_type`` and ``classification`` are derived from the order instruction
(FIX OrdType) only. ``intended_venue_*`` comes from the routing destination and
``actual_venue_id`` is always None on an order event — actual venue is an
execution fact (Contract D). The model validator enforces the invariant that
classification is a pure function of order type.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from venue_integrity.schemas.common import StrictModel, UtcDatetime
from venue_integrity.schemas.enums import Classification, MappingStatus, OrderType, Side

# Single source of truth for the classification function (spec §5.2):
# Classification ⊥ Venue Identity.
CLASSIFICATION_BY_ORDER_TYPE: dict[OrderType, Classification] = {
    OrderType.MARKET: Classification.MARKET,
    OrderType.LIMIT: Classification.LIMIT,
    OrderType.STOP: Classification.OTHER,
    OrderType.OTHER: Classification.OTHER,
    OrderType.UNKNOWN: Classification.UNRESOLVED,
}


class NormalizedOrder(StrictModel):
    event_id: str
    source_system: str
    session_id: str | None = None
    event_time: UtcDatetime
    cl_ord_id: str
    orig_cl_ord_id: str | None = None
    client_order_id: str | None = None
    parent_order_id: str | None = None
    child_order_id: str
    instrument_id: str
    side: Side
    order_quantity: Decimal = Field(gt=0)
    limit_price: Decimal | None = None
    raw_ord_type: str | None
    order_type: OrderType
    time_in_force: str | None = None
    intended_venue_id: str | None = None
    actual_venue_id: None = None
    raw_ex_destination: str | None = None
    raw_ex_destination_id_source: str | None = None
    raw_last_mkt: None = None
    classification: Classification
    mapping_status: MappingStatus
    mapping_reason: str
    mapping_version: str
    route_category: str | None = None
    normalized_at: UtcDatetime
    normalizer_version: str

    @model_validator(mode="after")
    def _invariants(self) -> NormalizedOrder:
        expected = CLASSIFICATION_BY_ORDER_TYPE[self.order_type]
        if self.classification is not expected:
            raise ValueError(
                f"classification {self.classification} inconsistent with order_type {self.order_type}"
            )
        if (self.mapping_status is MappingStatus.MAPPED) != (self.intended_venue_id is not None):
            raise ValueError("intended_venue_id must be set iff mapping_status is MAPPED")
        return self
