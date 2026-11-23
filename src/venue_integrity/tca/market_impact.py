"""Market-impact measures (spec §8.1).

* Temporary impact per fill: price paid beyond the consolidated touch
  (see :func:`venue_integrity.tca.spread.impact_beyond_touch_bps`).
* Permanent impact per parent order: side-adjusted midpoint move from the
  decision midpoint to the midpoint ``horizon`` after the last fill.
* A square-root benchmark  I = eta * sigma * sqrt(Q / ADV)  for comparing
  realised impact against an ex-ante expectation.
"""

from __future__ import annotations

import math

from venue_integrity.tca.spread import BPS, side_direction


def permanent_impact_bps(side, decision_mid: float, post_mid: float) -> float:
    if decision_mid <= 0 or post_mid <= 0:
        raise ValueError("prices must be positive")
    return side_direction(side) * (post_mid - decision_mid) / decision_mid * BPS


def square_root_impact_bps(quantity: float, adv: float, daily_vol_bps: float, eta: float = 1.0) -> float:
    if quantity < 0 or adv <= 0 or daily_vol_bps < 0:
        raise ValueError("invalid impact inputs")
    return eta * daily_vol_bps * math.sqrt(quantity / adv)
