"""Side-adjusted spread decomposition (spec §8.1–8.2).

    s   = +1 buy, -1 sell
    ES  = 2s (P_exec - M_0) / M_0 x 1e4      effective spread
    RS  = 2s (P_exec - M_h) / M_0 x 1e4      realised spread
    AS  = 2s (M_h   - M_0) / M_0 x 1e4       adverse selection (price impact)
    ES  = RS + AS                            (exact, all terms scaled by M_0)

Interpretation of the sign: AS > 0 when the midpoint subsequently moves in the
direction of the trade (up after a buy). That is adverse to the liquidity
provider; for an aggressive parent order it measures information leakage /
permanent impact that raises the cost of the remaining child orders. (The spec
text describes this as "against the aggressor"; the formula is unchanged —
only the wording is clarified here.)

M_0 is the consolidated midpoint prevailing at the execution timestamp. The
arrival midpoint (at child-order arrival) is used separately for slippage and
implementation shortfall.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from venue_integrity.schemas.enums import Side

BPS = 10_000.0


def side_direction(side: Side | str) -> int:
    """Strict side parsing: anything that is not a recognised side is an error,
    never silently treated as a sell."""
    if isinstance(side, Side):
        return side.direction
    try:
        return Side(str(side).upper()).direction
    except ValueError as exc:
        raise ValueError(f"unrecognised side {side!r}") from exc


@dataclass(frozen=True)
class ExecutionMetrics:
    effective_spread_bps: float
    realized_spread_bps: float
    adverse_selection_bps: float


def _check_prices(*prices: float) -> None:
    for p in prices:
        if p is None or not math.isfinite(p) or p <= 0:
            raise ValueError("Prices must be positive and finite")


def execution_metrics(side: Side | str, execution_price: float, arrival_mid: float, future_mid: float) -> ExecutionMetrics:
    """Spread decomposition for a single fill. ``arrival_mid`` is M_0."""
    _check_prices(execution_price, arrival_mid, future_mid)
    s = side_direction(side)
    es = 2 * s * (execution_price - arrival_mid) / arrival_mid * BPS
    rs = 2 * s * (execution_price - future_mid) / arrival_mid * BPS
    adverse = 2 * s * (future_mid - arrival_mid) / arrival_mid * BPS
    if not math.isclose(es, rs + adverse, rel_tol=1e-9, abs_tol=1e-9):  # pragma: no cover - algebraic identity
        raise ArithmeticError("ES != RS + AS")
    return ExecutionMetrics(es, rs, adverse)


def effective_spread_bps(side: Side | str, execution_price: float, mid: float) -> float:
    _check_prices(execution_price, mid)
    return 2 * side_direction(side) * (execution_price - mid) / mid * BPS


def impact_beyond_touch_bps(side: Side | str, execution_price: float, bid: float, ask: float, mid: float) -> float:
    """Temporary market impact: price paid beyond the consolidated touch.

    Negative values are price improvement inside the spread (e.g. midpoint fills)."""
    _check_prices(execution_price, bid, ask, mid)
    s = side_direction(side)
    touch = ask if s > 0 else bid
    return s * (execution_price - touch) / mid * BPS
