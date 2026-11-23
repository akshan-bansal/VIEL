"""Contract D — execution event (spec §3.4).

An execution must carry an attributable actual venue (from the execution
report's LastMkt) or an explicit unresolved status. The intended destination is
retained separately and is never used to fill ``actual_venue_id``.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from venue_integrity.schemas.common import StrictModel, UtcDatetime
from venue_integrity.schemas.enums import ExecType, MappingStatus, Side


class ExecutionEvent(StrictModel):
    exec_id: str
    exec_ref_id: str | None = None
    exec_type: ExecType
    raw_exec_type: str
    raw_exec_trans_type: str | None = None
    source_event_id: str
    source_system: str
    session_id: str | None = None
    sequence_number: int | None = None
    poss_dup: bool = False
    order_id: str | None = None
    child_order_id: str | None = None
    parent_order_id: str | None = None
    instrument_id: str
    side: Side
    ord_status: str | None = None
    raw_last_mkt: str | None = None
    actual_venue_id: str | None = None
    venue_mic: str | None = None
    mapping_status: MappingStatus
    mapping_reason: str
    mapping_version: str
    executed_quantity: Decimal = Field(ge=0)
    execution_price: Decimal | None = None
    cum_qty: Decimal | None = None
    leaves_qty: Decimal | None = None
    execution_timestamp: UtcDatetime
    execution_timestamp_ns: int
    payload_hash: str

    @model_validator(mode="after")
    def _invariants(self) -> ExecutionEvent:
        if (self.mapping_status is MappingStatus.MAPPED) != (self.actual_venue_id is not None):
            raise ValueError("actual_venue_id must be set iff mapping_status is MAPPED")
        if self.exec_type in (ExecType.TRADE, ExecType.TRADE_CORRECT):
            if self.executed_quantity <= 0:
                raise ValueError(f"{self.exec_type} requires executed_quantity > 0")
            if self.execution_price is None or self.execution_price <= 0:
                raise ValueError(f"{self.exec_type} requires a positive execution_price")
        if self.exec_type in (ExecType.TRADE_CORRECT, ExecType.TRADE_CANCEL) and not self.exec_ref_id:
            raise ValueError(f"{self.exec_type} requires exec_ref_id")
        return self

    def economic_fingerprint(self) -> tuple:
        """Fields that define the economic content of the report (duplicate detection)."""
        return (
            self.exec_type,
            self.exec_ref_id,
            self.child_order_id,
            self.instrument_id,
            self.side,
            self.executed_quantity,
            self.execution_price,
            self.raw_last_mkt,
            self.execution_timestamp_ns,
        )
