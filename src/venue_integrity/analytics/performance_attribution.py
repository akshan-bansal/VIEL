"""Attribution of parent-order implementation shortfall to execution venues.

Each parent's execution-cost component is split across venues in proportion to
the notional executed there; fee and opportunity components stay at parent
level. Unresolved venues keep their own bucket.
"""

from __future__ import annotations

from collections import defaultdict

from venue_integrity.tca.engine import TcaReport


def attribute_shortfall(tca: TcaReport) -> dict[str, dict[str, float]]:
    fills_by_parent = defaultdict(list)
    for f in tca.fills:
        if f.included and f.parent_order_id:
            fills_by_parent[f.parent_order_id].append(f)
    contribution: dict[str, float] = defaultdict(float)
    notional: dict[str, float] = defaultdict(float)
    for parent in tca.parents:
        fills = fills_by_parent.get(parent.parent_order_id, [])
        total = sum(f.notional for f in fills)
        if total <= 0:
            continue
        for f in fills:
            share = f.notional / total
            contribution[f.venue_key] += parent.execution_cost_bps * share * f.notional
            notional[f.venue_key] += f.notional
    return {
        venue: {"notional": round(notional[venue], 2),
                "notional_weighted_execution_cost_bps": round(contribution[venue] / notional[venue], 6)}
        for venue in sorted(notional) if notional[venue] > 0
    }
