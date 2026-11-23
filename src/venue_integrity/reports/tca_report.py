"""Plain-text / Markdown TCA tables."""

from __future__ import annotations

from typing import Any


def _f(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def venue_tca_table(tca: dict[str, Any], reference_horizon_ms: int) -> str:
    h = str(reference_horizon_ms)
    lines = [
        f"| Venue | Fills | Qty | Eff. spread (bps) | One-way cost (bps) | Fee-adj. cost (bps) | Impact beyond touch (bps) "
        f"| RS@{h}ms | AS@{h}ms | AS@5s | Incomplete@30s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for venue, v in sorted(tca["venues"].items()):
        ref = v["horizons"].get(h, {})
        five = v["horizons"].get("5000", {})
        thirty = v["horizons"].get("30000", {})
        lines.append(
            f"| {venue} | {v['fills']} | {int(v['quantity'])} | {_f(v['effective_spread_bps'])} | {_f(v['effective_cost_bps'])} "
            f"| {_f(v['fee_adjusted_cost_bps'])} | {_f(v['impact_beyond_touch_bps'])} | {_f(ref.get('realized_spread_bps'))} "
            f"| {_f(ref.get('adverse_selection_bps'))} | {_f(five.get('adverse_selection_bps'))} | {thirty.get('incomplete', 0)} |"
        )
    return "\n".join(lines)
