"""Venue-specific transaction-cost analysis (spec §8).

Attribution controls (spec §8.3):

* fills are attributed to their *actual* execution venue; unresolved venues
  form their own ``UNRESOLVED`` bucket and are never merged into a known venue;
* the intended destination is retained alongside;
* only reconciled, non-busted, non-ambiguous trades are eligible;
* duplicates were already removed by reconciliation, so nothing is double counted;
* stale reference quotes exclude the fill (with a finding), incomplete markout
  horizons are reported as INCOMPLETE — never as zero;
* gross and fee-adjusted costs are reported separately; an unknown fee leaves
  the fee-adjusted figure empty rather than assuming zero.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from venue_integrity.execution.execution_reconciler import UNRESOLVED, ReconciliationReport, TradeStatus
from venue_integrity.identity.exception_queue import Finding
from venue_integrity.schemas.enums import ExceptionType
from venue_integrity.tca.implementation_shortfall import implementation_shortfall
from venue_integrity.tca.market_impact import permanent_impact_bps
from venue_integrity.tca.markout import MarketData, QuoteStatus
from venue_integrity.tca.spread import BPS, execution_metrics, impact_beyond_touch_bps

MS = 1_000_000


@dataclass
class HorizonMetric:
    status: QuoteStatus
    realized_spread_bps: float | None = None
    adverse_selection_bps: float | None = None


@dataclass
class FillMetrics:
    exec_id: str
    source_system: str
    child_order_id: str | None
    parent_order_id: str | None
    instrument_id: str
    side: str
    venue_key: str
    intended_venue_id: str | None
    quantity: float
    price: float
    included: bool
    trade_timestamp_ns: int = 0
    exclusion_reason: str | None = None
    mid_at_execution: float | None = None
    arrival_mid: float | None = None
    effective_spread_bps: float | None = None
    effective_cost_bps: float | None = None
    impact_beyond_touch_bps: float | None = None
    slippage_vs_arrival_bps: float | None = None
    fee_bps: float | None = None
    fee_adjusted_cost_bps: float | None = None
    horizons: dict[int, HorizonMetric] = field(default_factory=dict)

    @property
    def notional(self) -> float:
        return self.quantity * self.price


@dataclass
class VenueTca:
    venue_key: str
    fills: int
    quantity: float
    notional: float
    effective_spread_bps: float | None
    effective_cost_bps: float | None
    fee_adjusted_cost_bps: float | None
    impact_beyond_touch_bps: float | None
    horizons: dict[int, dict[str, Any]]
    excluded: dict[str, int]

    def as_bps(self, horizon_ms: int) -> float | None:
        return self.horizons.get(horizon_ms, {}).get("adverse_selection_bps")

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue_key,
            "fills": self.fills,
            "quantity": self.quantity,
            "notional": round(self.notional, 2),
            "effective_spread_bps": _r(self.effective_spread_bps),
            "effective_cost_bps": _r(self.effective_cost_bps),
            "fee_adjusted_cost_bps": _r(self.fee_adjusted_cost_bps),
            "impact_beyond_touch_bps": _r(self.impact_beyond_touch_bps),
            "horizons": {str(h): {k: _r(v) if isinstance(v, float) else v for k, v in m.items()}
                         for h, m in sorted(self.horizons.items())},
            "excluded": dict(sorted(self.excluded.items())),
        }


@dataclass
class ParentTca:
    parent_order_id: str
    side: str
    ordered_quantity: float
    executed_quantity: float
    decision_mid: float
    implementation_shortfall_bps: float
    execution_cost_bps: float
    fee_bps: float
    opportunity_cost_bps: float
    permanent_impact_bps: float | None

    def to_dict(self) -> dict[str, Any]:
        return {k: (_r(v) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class TcaReport:
    fills: list[FillMetrics]
    venues: dict[str, VenueTca]
    parents: list[ParentTca]
    intended_venue_fill_ratio: dict[str, float]
    coverage: dict[str, Any]
    findings: list[Finding]
    horizons_ms: list[int]
    reference_horizon_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizons_ms": self.horizons_ms,
            "reference_horizon_ms": self.reference_horizon_ms,
            "coverage": self.coverage,
            "venues": {k: v.to_dict() for k, v in sorted(self.venues.items())},
            "intended_venue_fill_ratio": {k: round(v, 6) for k, v in sorted(self.intended_venue_fill_ratio.items())},
            "parents_summary": _parent_summary(self.parents),
        }


def _r(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


def _parent_summary(parents: list[ParentTca]) -> dict[str, Any]:
    if not parents:
        return {"parents": 0}
    total_q = sum(p.ordered_quantity * p.decision_mid for p in parents)
    weighted = sum(p.implementation_shortfall_bps * p.ordered_quantity * p.decision_mid for p in parents) / total_q
    return {
        "parents": len(parents),
        "notional_weighted_is_bps": round(weighted, 6),
        "mean_is_bps": round(sum(p.implementation_shortfall_bps for p in parents) / len(parents), 6),
    }


def _wmean(pairs: list[tuple[float, float]]) -> float | None:
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    return sum(v * w for v, w in pairs) / total


class TcaEngine:
    def __init__(
        self,
        market_data: MarketData,
        *,
        horizons_ms: list[int],
        reference_horizon_ms: int,
        max_staleness_ms: int,
        fee_schedule_bps: dict[str, float],
    ):
        if reference_horizon_ms not in horizons_ms:
            raise ValueError("reference horizon must be one of the horizons")
        self.md = market_data
        self.horizons_ms = sorted(horizons_ms)
        self.reference_horizon_ms = reference_horizon_ms
        self.max_staleness_ns = max_staleness_ms * MS
        self.fees = dict(fee_schedule_bps)

    def run(self, reconciliation: ReconciliationReport) -> TcaReport:
        findings: list[Finding] = []
        fills: list[FillMetrics] = []
        arrival_by_child = {(c.source_system, c.child_order_id): c.arrival_time_ns for c in reconciliation.children}

        for e in reconciliation.executions:
            fm = FillMetrics(
                exec_id=e.original_exec_id,
                source_system=e.source_system,
                child_order_id=e.child_order_id,
                parent_order_id=e.parent_order_id,
                instrument_id=e.instrument_id,
                side=e.side.value,
                venue_key=e.venue_key,
                intended_venue_id=e.intended_venue_id,
                quantity=float(e.quantity if e.status is TradeStatus.ACTIVE else e.reported_quantity),
                price=float(e.price) if e.price is not None else 0.0,
                included=False,
                trade_timestamp_ns=e.trade_timestamp_ns,
            )
            fills.append(fm)
            if e.status is not TradeStatus.ACTIVE:
                fm.exclusion_reason = e.status.value  # BUSTED / AMBIGUOUS: not eligible
                continue
            series = self.md.consolidated.get(e.instrument_id)
            if series is None:
                fm.exclusion_reason = "NO_MARKET_DATA"
                continue
            t = e.trade_timestamp_ns
            at_exec = series.asof(t, self.max_staleness_ns)
            if at_exec.status is not QuoteStatus.OK:
                fm.exclusion_reason = f"{at_exec.status.value}_MIDPOINT_AT_EXECUTION"
                if at_exec.status is QuoteStatus.STALE:
                    findings.append(Finding(ExceptionType.STALE_MIDPOINT,
                                            f"fill {e.original_exec_id}: reference quote "
                                            f"{(t - (at_exec.quote_ts_ns or t)) / MS:.0f} ms old; excluded from TCA",
                                            source_event_id=e.source_event_ids[0], entity_key=e.original_exec_id))
                continue
            m0 = at_exec.mid
            fm.included = True
            fm.mid_at_execution = m0
            price = float(e.price)
            fm.effective_spread_bps = execution_metrics(e.side, price, m0, m0).effective_spread_bps
            fm.effective_cost_bps = fm.effective_spread_bps / 2
            fm.impact_beyond_touch_bps = impact_beyond_touch_bps(e.side, price, at_exec.bid, at_exec.ask, m0)
            fee = self.fees.get(e.venue_key) if e.venue_key != UNRESOLVED else None
            fm.fee_bps = fee
            fm.fee_adjusted_cost_bps = fm.effective_cost_bps + fee if fee is not None else None
            arrival_ns = arrival_by_child.get((e.source_system, e.child_order_id))
            if arrival_ns is not None:
                arrival = series.asof(arrival_ns, self.max_staleness_ns)
                if arrival.status is QuoteStatus.OK:
                    fm.arrival_mid = arrival.mid
                    fm.slippage_vs_arrival_bps = e.side.direction * (price - arrival.mid) / arrival.mid * BPS
            for h in self.horizons_ms:
                look = series.markout(t, h * MS, self.max_staleness_ns)
                if look.status is QuoteStatus.OK:
                    metrics = execution_metrics(e.side, price, m0, look.mid)
                    fm.horizons[h] = HorizonMetric(QuoteStatus.OK, metrics.realized_spread_bps, metrics.adverse_selection_bps)
                else:
                    fm.horizons[h] = HorizonMetric(look.status)

        venues = self._aggregate(fills)
        parents, parents_excluded = self._parents(reconciliation, fills)
        fill_ratio = self._intended_fill_ratio(reconciliation)
        eligible = [f for f in fills if f.exclusion_reason not in ("BUSTED", "AMBIGUOUS")]
        computed = [f for f in eligible if f.included]
        incomplete = {h: sum(1 for f in computed if f.horizons[h].status is QuoteStatus.INCOMPLETE) for h in self.horizons_ms}
        stale_h = {h: sum(1 for f in computed if f.horizons[h].status is QuoteStatus.STALE) for h in self.horizons_ms}
        coverage = {
            "eligible_fills": len(eligible),
            "computed_fills": len(computed),
            "coverage_ratio": len(computed) / len(eligible) if eligible else 1.0,
            "excluded_by_reason": dict(sorted(_count(f.exclusion_reason for f in fills if not f.included).items())),
            "incomplete_horizons": {str(h): n for h, n in incomplete.items()},
            "stale_horizons": {str(h): n for h, n in stale_h.items()},
            "stale_midpoints_included": 0,
            "unresolved_venue_fills": sum(1 for f in computed if f.venue_key == UNRESOLVED),
            "parents_computed": len(parents),
            "parents_excluded_by_reason": dict(sorted(parents_excluded.items())),
        }
        findings.sort(key=lambda f: f.dedup_key)
        return TcaReport(fills, venues, parents, fill_ratio, coverage, findings, self.horizons_ms, self.reference_horizon_ms)

    def _aggregate(self, fills: list[FillMetrics]) -> dict[str, VenueTca]:
        by_venue: dict[str, list[FillMetrics]] = defaultdict(list)
        for f in fills:
            by_venue[f.venue_key].append(f)
        out = {}
        for venue, group in sorted(by_venue.items()):
            used = [f for f in group if f.included]
            horizons = {}
            for h in self.horizons_ms:
                complete = [f for f in used if f.horizons[h].status is QuoteStatus.OK]
                horizons[h] = {
                    "complete": len(complete),
                    "incomplete": sum(1 for f in used if f.horizons[h].status is QuoteStatus.INCOMPLETE),
                    "stale": sum(1 for f in used if f.horizons[h].status is QuoteStatus.STALE),
                    "realized_spread_bps": _wmean([(f.horizons[h].realized_spread_bps, f.quantity) for f in complete]),
                    "adverse_selection_bps": _wmean([(f.horizons[h].adverse_selection_bps, f.quantity) for f in complete]),
                }
            fee_known = [f for f in used if f.fee_adjusted_cost_bps is not None]
            out[venue] = VenueTca(
                venue_key=venue,
                fills=len(used),
                quantity=sum(f.quantity for f in used),
                notional=sum(f.notional for f in used),
                effective_spread_bps=_wmean([(f.effective_spread_bps, f.quantity) for f in used]),
                effective_cost_bps=_wmean([(f.effective_cost_bps, f.quantity) for f in used]),
                fee_adjusted_cost_bps=_wmean([(f.fee_adjusted_cost_bps, f.quantity) for f in fee_known])
                if len(fee_known) == len(used) else None,
                impact_beyond_touch_bps=_wmean([(f.impact_beyond_touch_bps, f.quantity) for f in used]),
                horizons=horizons,
                excluded=dict(_count(f.exclusion_reason for f in group if not f.included)),
            )
        return out

    def _parents(self, rec: ReconciliationReport, fills: list[FillMetrics]) -> tuple[list[ParentTca], dict[str, int]]:
        children_by_parent = defaultdict(list)
        for c in rec.children:
            if c.parent_order_id:
                children_by_parent[c.parent_order_id].append(c)
        fills_by_parent = defaultdict(list)
        times = {(e.source_system, e.original_exec_id): e.trade_timestamp_ns for e in rec.executions}
        for f in fills:
            if f.included and f.parent_order_id:
                fills_by_parent[f.parent_order_id].append(f)
        out = []
        excluded: dict[str, int] = defaultdict(int)
        ref_ns = self.reference_horizon_ms * MS
        for parent_id in sorted(children_by_parent):
            children = children_by_parent[parent_id]
            instrument = children[0].instrument_id
            series = self.md.consolidated.get(instrument)
            if series is None:
                excluded["NO_MARKET_DATA"] += 1
                continue
            sides = {c.side for c in children}
            if len(sides) != 1:
                excluded["MIXED_SIDES"] += 1
                continue
            side = children[0].side
            start = min(c.arrival_time_ns for c in children)
            decision = series.asof(start, self.max_staleness_ns)
            if decision.status is not QuoteStatus.OK:
                excluded[f"DECISION_MIDPOINT_{decision.status.value}"] += 1
                continue
            pf = fills_by_parent.get(parent_id, [])
            if any(f.fee_bps is None for f in pf):
                excluded["FEE_UNKNOWN_UNRESOLVED_VENUE"] += 1  # not computable without assuming a fee
                continue
            last = max([times[(f.source_system, f.exec_id)] for f in pf] + [max(c.arrival_time_ns for c in children)])
            end = series.asof(last, self.max_staleness_ns)
            if end.status is not QuoteStatus.OK:
                excluded[f"END_MIDPOINT_{end.status.value}"] += 1
                continue
            result = implementation_shortfall(
                side, float(sum(c.ordered_quantity for c in children)), decision.mid, end.mid,
                [(f.quantity, f.price, f.fee_bps) for f in pf],
            )
            post = series.markout(last, ref_ns, self.max_staleness_ns)
            impact = permanent_impact_bps(side, decision.mid, post.mid) if post.status is QuoteStatus.OK else None
            out.append(ParentTca(parent_id, side.value, result.ordered_quantity, result.executed_quantity,
                                 decision.mid, result.total_bps, result.execution_cost_bps, result.fee_bps,
                                 result.opportunity_cost_bps, impact))
        return out, dict(excluded)

    @staticmethod
    def _intended_fill_ratio(rec: ReconciliationReport) -> dict[str, float]:
        ordered: dict[str, float] = defaultdict(float)
        filled: dict[str, float] = defaultdict(float)
        for c in rec.children:
            key = c.intended_venue_id or UNRESOLVED
            ordered[key] += float(c.ordered_quantity)
            filled[key] += float(c.filled_quantity)
        return {k: filled[k] / ordered[k] for k in ordered if ordered[k] > 0}


def _count(values) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for v in values:
        out[str(v)] += 1
    return out
