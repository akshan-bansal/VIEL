"""Implementation shortfall (Perold) for a parent order (spec §8.1).

    IS = [ s * sum_i q_i (p_i - P_d)  +  fees  +  s * (Q - X)(P_end - P_d) ] / (Q * P_d)   (x 1e4 bps)

with P_d the decision (arrival) midpoint, X the executed quantity, Q the
ordered quantity and P_end the midpoint when the parent order finished. The
three components (execution cost, fees, opportunity cost) are reported
separately so that gross and fee-adjusted performance are never conflated.
"""

from __future__ import annotations

from dataclasses import dataclass

from venue_integrity.tca.spread import BPS, side_direction


@dataclass(frozen=True)
class ShortfallResult:
    ordered_quantity: float
    executed_quantity: float
    decision_price: float
    end_price: float
    average_price: float | None
    execution_cost_bps: float
    fee_bps: float
    opportunity_cost_bps: float

    @property
    def total_bps(self) -> float:
        return self.execution_cost_bps + self.fee_bps + self.opportunity_cost_bps

    @property
    def fill_ratio(self) -> float:
        return self.executed_quantity / self.ordered_quantity if self.ordered_quantity else 0.0


def implementation_shortfall(
    side,
    ordered_quantity: float,
    decision_price: float,
    end_price: float,
    fills: list[tuple[float, float, float | None]],
) -> ShortfallResult:
    """``fills`` = [(quantity, price, fee_bps or None)]. Unknown fees raise —
    a missing fee schedule is a data problem, not a zero fee."""
    if ordered_quantity <= 0 or decision_price <= 0 or end_price <= 0:
        raise ValueError("ordered quantity and prices must be positive")
    s = side_direction(side)
    denominator = ordered_quantity * decision_price
    executed = sum(q for q, _, _ in fills)
    execution_cost = sum(s * q * (p - decision_price) for q, p, _ in fills)
    if any(f is None for _, _, f in fills):
        raise ValueError("fee unknown for at least one fill")
    fees = sum(q * p * (f / BPS) for q, p, f in fills)
    unfilled = max(ordered_quantity - executed, 0.0)
    opportunity = s * unfilled * (end_price - decision_price)
    avg = (sum(q * p for q, p, _ in fills) / executed) if executed else None
    return ShortfallResult(
        ordered_quantity=ordered_quantity,
        executed_quantity=executed,
        decision_price=decision_price,
        end_price=end_price,
        average_price=avg,
        execution_cost_bps=execution_cost / denominator * BPS,
        fee_bps=fees / denominator * BPS,
        opportunity_cost_bps=opportunity / denominator * BPS,
    )
