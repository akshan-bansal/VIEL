"""Parent / child / fill reconciliation (spec §6).

The reconciler is set-based: its result is a function of the *set* of order and
execution events, so delivering the same events in a different order, or
delivering a message twice, cannot change reconciled quantities (spec §11.3
invariants 4 and 5).

Economic state of a trade follows its reference chain, not arrival order:

    TRADE(T1) <- TRADE_CORRECT(C1, ref T1) <- TRADE_CORRECT(C2, ref C1)

The effective trade is the chain's terminal correction; a TRADE_CANCEL anywhere
on the chain busts it; a fork (two modifications of the same version) is
AMBIGUOUS and excluded from economic totals with an exception.

The venue of a fill is the fill's own attributed venue. Nothing here reads the
parent's or the child's intended destination to fill in an execution venue.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from venue_integrity.identity.exception_queue import Finding
from venue_integrity.schemas.enums import Classification, ExceptionType, ExecType, MappingStatus, OrderType, Side
from venue_integrity.schemas.execution_event import ExecutionEvent
from venue_integrity.schemas.normalized_order import NormalizedOrder

ZERO = Decimal(0)
UNRESOLVED = "UNRESOLVED"


class TradeStatus(StrEnum):
    ACTIVE = "ACTIVE"
    BUSTED = "BUSTED"
    AMBIGUOUS = "AMBIGUOUS"


class ChildStatus(StrEnum):
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PARTIALLY_FILLED_CANCELED = "PARTIALLY_FILLED_CANCELED"
    UNFILLED_OPEN = "UNFILLED_OPEN"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    OVERFILLED = "OVERFILLED"


_TERMINAL = (ExecType.CANCELED, ExecType.REJECTED, ExecType.EXPIRED, ExecType.DONE_FOR_DAY)


@dataclass(frozen=True)
class EffectiveExecution:
    original_exec_id: str
    effective_exec_id: str
    chain: tuple[str, ...]
    status: TradeStatus
    source_system: str
    child_order_id: str | None
    parent_order_id: str | None
    instrument_id: str
    side: Side
    quantity: Decimal
    reported_quantity: Decimal
    price: Decimal | None
    actual_venue_id: str | None
    venue_mic: str | None
    raw_last_mkt: str | None
    mapping_status: MappingStatus
    mapping_version: str
    trade_timestamp_ns: int
    source_event_ids: tuple[str, ...]
    intended_venue_id: str | None = None
    order_type: OrderType | None = None
    classification: Classification | None = None
    lineage_status: str = "COMPLETE"

    @property
    def venue_key(self) -> str:
        return self.actual_venue_id if self.mapping_status is MappingStatus.MAPPED and self.actual_venue_id else UNRESOLVED

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_exec_id": self.original_exec_id,
            "effective_exec_id": self.effective_exec_id,
            "chain": list(self.chain),
            "status": self.status.value,
            "source_system": self.source_system,
            "child_order_id": self.child_order_id,
            "parent_order_id": self.parent_order_id,
            "instrument_id": self.instrument_id,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "price": str(self.price) if self.price is not None else None,
            "actual_venue_id": self.actual_venue_id,
            "raw_last_mkt": self.raw_last_mkt,
            "mapping_status": self.mapping_status.value,
            "intended_venue_id": self.intended_venue_id,
            "order_type": self.order_type.value if self.order_type else None,
            "lineage_status": self.lineage_status,
        }


@dataclass
class ChildReconciliation:
    source_system: str
    child_order_id: str
    parent_order_id: str | None
    client_order_id: str | None
    instrument_id: str
    side: Side
    order_type: OrderType
    classification: Classification
    ordered_quantity: Decimal
    filled_quantity: Decimal
    status: ChildStatus
    intended_venue_id: str | None
    intended_mapping_status: MappingStatus
    raw_ex_destination: str | None
    venue_breakdown: dict[str, Decimal]
    fill_count: int
    reported_cum_qty: Decimal | None
    order_event_id: str
    arrival_time_ns: int
    exception_types: list[str] = field(default_factory=list)

    @property
    def fill_ratio(self) -> float:
        return float(self.filled_quantity / self.ordered_quantity) if self.ordered_quantity else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_system": self.source_system,
            "child_order_id": self.child_order_id,
            "parent_order_id": self.parent_order_id,
            "client_order_id": self.client_order_id,
            "instrument_id": self.instrument_id,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "classification": self.classification.value,
            "ordered_quantity": str(self.ordered_quantity),
            "filled_quantity": str(self.filled_quantity),
            "status": self.status.value,
            "intended_venue_id": self.intended_venue_id,
            "intended_mapping_status": self.intended_mapping_status.value,
            "raw_ex_destination": self.raw_ex_destination,
            "venue_breakdown": {k: str(v) for k, v in sorted(self.venue_breakdown.items())},
            "fill_count": self.fill_count,
            "reported_cum_qty": str(self.reported_cum_qty) if self.reported_cum_qty is not None else None,
            "exception_types": sorted(set(self.exception_types)),
        }


@dataclass
class ParentReconciliation:
    parent_order_id: str
    child_order_ids: list[str]
    ordered_quantity: Decimal
    filled_quantity: Decimal
    venue_breakdown: dict[str, Decimal]
    statuses: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_order_id": self.parent_order_id,
            "child_order_ids": self.child_order_ids,
            "ordered_quantity": str(self.ordered_quantity),
            "filled_quantity": str(self.filled_quantity),
            "venue_breakdown": {k: str(v) for k, v in sorted(self.venue_breakdown.items())},
            "statuses": dict(sorted(self.statuses.items())),
        }


@dataclass
class ReconciliationReport:
    children: list[ChildReconciliation]
    parents: list[ParentReconciliation]
    executions: list[EffectiveExecution]
    findings: list[Finding]
    kpis: dict[str, Any]

    def child(self, child_order_id: str, source_system: str | None = None) -> ChildReconciliation:
        for c in self.children:
            if c.child_order_id == child_order_id and (source_system is None or c.source_system == source_system):
                return c
        raise KeyError(child_order_id)

    def economic_state(self) -> dict[str, Any]:
        """Order-independent summary used by determinism tests."""
        return {
            "children": {f"{c.source_system}|{c.child_order_id}": (str(c.filled_quantity), c.status.value,
                                                                     tuple(sorted((k, str(v)) for k, v in c.venue_breakdown.items())))
                         for c in self.children},
            "executions": sorted((e.source_system, e.original_exec_id, e.status.value, str(e.quantity),
                                  str(e.price), e.venue_key) for e in self.executions),
        }


def _key(source_system: str, ident: str | None) -> tuple[str, str]:
    return (source_system, ident or "")


def reconcile(orders: list[NormalizedOrder], executions: list[ExecutionEvent]) -> ReconciliationReport:
    findings: list[Finding] = []

    # -- orders (dedupe re-sent NewOrderSingle) ------------------------------
    orders_by_key: dict[tuple[str, str], list[NormalizedOrder]] = defaultdict(list)
    for order in orders:
        orders_by_key[_key(order.source_system, order.cl_ord_id)].append(order)
    order_index: dict[tuple[str, str], NormalizedOrder] = {}
    for key, group in orders_by_key.items():
        group = sorted(group, key=lambda o: o.event_id)
        canonical = group[0]
        fingerprints = {(o.side, o.order_quantity, o.raw_ord_type, o.raw_ex_destination, o.instrument_id) for o in group}
        if len(fingerprints) > 1:
            findings.append(Finding(ExceptionType.MISSING_LINEAGE,
                                    f"ClOrdID {key[1]} re-used with different order content",
                                    source_event_id=canonical.event_id, entity_key=key[1]))
        order_index[key] = canonical

    # -- execution de-duplication -----------------------------------------
    economic = [e for e in executions if e.exec_type.is_economic]
    status_events = [e for e in executions if not e.exec_type.is_economic]
    by_exec: dict[tuple[str, str], list[ExecutionEvent]] = defaultdict(list)
    for event in economic:
        by_exec[_key(event.source_system, event.exec_id)].append(event)

    unique: dict[tuple[str, str], ExecutionEvent] = {}
    conflicted: set[tuple[str, str]] = set()
    duplicate_messages = 0
    duplicate_quantity = ZERO
    conflicted_quantity = ZERO
    for key in sorted(by_exec):
        group = sorted(by_exec[key], key=lambda e: e.source_event_id)
        fingerprints = {e.economic_fingerprint() for e in group}
        if len(fingerprints) > 1:
            conflicted.add(key)
            conflicted_quantity += sum((e.executed_quantity for e in group), ZERO)
            findings.append(Finding(ExceptionType.CONFLICTING_EXEC_ID,
                                    f"ExecID {key[1]} reported {len(group)} times with different economic content; "
                                    f"excluded from economic totals",
                                    source_event_id=group[0].source_event_id, entity_key=key[1],
                                    context={"source_event_ids": [e.source_event_id for e in group]}))
            continue
        unique[key] = group[0]
        if len(group) > 1:
            duplicate_messages += len(group) - 1
            duplicate_quantity += group[0].executed_quantity * (len(group) - 1)
            findings.append(Finding(ExceptionType.DUPLICATE_EXEC_ID,
                                    f"ExecID {key[1]} received {len(group)} times; counted once",
                                    source_event_id=group[0].source_event_id, entity_key=key[1],
                                    context={"duplicate_event_ids": [e.source_event_id for e in group[1:]]}))

    # -- correction / bust chains -----------------------------------------
    modifications: dict[tuple[str, str], list[ExecutionEvent]] = defaultdict(list)
    for event in unique.values():
        if event.exec_type in (ExecType.TRADE_CORRECT, ExecType.TRADE_CANCEL):
            modifications[_key(event.source_system, event.exec_ref_id)].append(event)

    visited_mods: set[tuple[str, str]] = set()
    effective: list[EffectiveExecution] = []
    for key in sorted(k for k, e in unique.items() if e.exec_type is ExecType.TRADE):
        root = unique[key]
        node = root
        chain = [root]
        status = TradeStatus.ACTIVE
        seen = {key}
        while True:
            mods = sorted(modifications.get(_key(node.source_system, node.exec_id), []), key=lambda e: e.exec_id)
            for m in mods:
                visited_mods.add(_key(m.source_system, m.exec_id))
            if not mods:
                break
            if len(mods) > 1:
                status = TradeStatus.AMBIGUOUS
                chain.extend(mods)
                findings.append(Finding(ExceptionType.AMBIGUOUS_CORRECTION,
                                        f"exec {node.exec_id} modified by {len(mods)} reports "
                                        f"({', '.join(m.exec_id for m in mods)}); trade excluded",
                                        source_event_id=root.source_event_id, entity_key=root.exec_id))
                break
            mod = mods[0]
            mod_key = _key(mod.source_system, mod.exec_id)
            if mod_key in seen:  # pragma: no cover - defensive cycle guard
                status = TradeStatus.AMBIGUOUS
                break
            seen.add(mod_key)
            chain.append(mod)
            if mod.exec_type is ExecType.TRADE_CANCEL:
                status = TradeStatus.BUSTED
                break
            node = mod
        terminal = node
        quantity = terminal.executed_quantity if status is TradeStatus.ACTIVE else ZERO
        effective.append(
            EffectiveExecution(
                original_exec_id=root.exec_id,
                effective_exec_id=terminal.exec_id,
                chain=tuple(e.exec_id for e in chain),
                status=status,
                source_system=root.source_system,
                child_order_id=root.child_order_id,
                parent_order_id=root.parent_order_id,
                instrument_id=terminal.instrument_id,
                side=root.side,
                quantity=quantity,
                reported_quantity=terminal.executed_quantity,
                price=terminal.execution_price,
                actual_venue_id=terminal.actual_venue_id,
                venue_mic=terminal.venue_mic,
                raw_last_mkt=terminal.raw_last_mkt,
                mapping_status=terminal.mapping_status,
                mapping_version=terminal.mapping_version,
                trade_timestamp_ns=root.execution_timestamp_ns,
                source_event_ids=tuple(e.source_event_id for e in chain),
            )
        )

    for key, event in sorted(unique.items()):
        if event.exec_type in (ExecType.TRADE_CORRECT, ExecType.TRADE_CANCEL) and key not in visited_mods:
            findings.append(Finding(ExceptionType.ORPHAN_CORRECTION,
                                    f"{event.exec_type} {event.exec_id} references unknown ExecID {event.exec_ref_id}",
                                    source_event_id=event.source_event_id, entity_key=event.exec_id,
                                    raw_value=event.exec_ref_id))

    # -- attach lineage ---------------------------------------------------
    attached: list[EffectiveExecution] = []
    for e in effective:
        order = order_index.get(_key(e.source_system, e.child_order_id))
        if order is None:
            findings.append(Finding(ExceptionType.ORPHAN_EXECUTION,
                                    f"execution {e.original_exec_id} has no matching child order {e.child_order_id!r}",
                                    source_event_id=e.source_event_ids[0], entity_key=e.original_exec_id))
            attached.append(_replace(e, lineage_status="ORPHAN"))
            continue
        parent = e.parent_order_id or order.parent_order_id
        lineage = "COMPLETE" if parent else "MISSING_PARENT"
        attached.append(_replace(e, parent_order_id=parent, intended_venue_id=order.intended_venue_id,
                                 order_type=order.order_type, classification=order.classification,
                                 lineage_status=lineage))
    effective = sorted(attached, key=lambda e: (e.trade_timestamp_ns, e.source_system, e.original_exec_id))

    # -- per child --------------------------------------------------------
    fills_by_child: dict[tuple[str, str], list[EffectiveExecution]] = defaultdict(list)
    for e in effective:
        fills_by_child[_key(e.source_system, e.child_order_id)].append(e)
    status_by_child: dict[tuple[str, str], list[ExecutionEvent]] = defaultdict(list)
    for event in status_events:
        status_by_child[_key(event.source_system, event.child_order_id)].append(event)
    reports_by_child: dict[tuple[str, str], list[ExecutionEvent]] = defaultdict(list)
    for event in [*unique.values(), *status_events]:
        reports_by_child[_key(event.source_system, event.child_order_id)].append(event)

    children: list[ChildReconciliation] = []
    for key in sorted(order_index):
        order = order_index[key]
        fills = fills_by_child.get(key, [])
        active = [f for f in fills if f.status is TradeStatus.ACTIVE]
        filled = sum((f.quantity for f in active), ZERO)
        breakdown: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for f in active:
            breakdown[f.venue_key] += f.quantity
        child_findings: list[Finding] = []

        statuses = sorted(status_by_child.get(key, []), key=lambda e: (e.execution_timestamp_ns, e.exec_id))
        if any(e.exec_type is ExecType.REPLACED for e in statuses):
            child_findings.append(Finding(ExceptionType.UNSUPPORTED_ORDER_EVENT,
                                          f"child {order.cl_ord_id} was replaced; replace chains are not reconciled "
                                          f"by the prototype", source_event_id=order.event_id, entity_key=order.cl_ord_id))
        terminal = next((e for e in statuses if e.exec_type in _TERMINAL), None)

        if terminal is not None and active:
            late = [f for f in active if f.trade_timestamp_ns > terminal.execution_timestamp_ns]
            if terminal.exec_type is ExecType.REJECTED or late:
                child_findings.append(Finding(ExceptionType.FILL_AFTER_TERMINAL_STATE,
                                              f"child {order.cl_ord_id} has fills inconsistent with terminal state "
                                              f"{terminal.exec_type}", source_event_id=terminal.source_event_id,
                                              entity_key=order.cl_ord_id))

        qty = order.order_quantity
        if filled > qty:
            status = ChildStatus.OVERFILLED
            child_findings.append(Finding(ExceptionType.OVERFILL,
                                          f"child {order.cl_ord_id} filled {filled} > ordered {qty}",
                                          source_event_id=order.event_id, entity_key=order.cl_ord_id))
        elif filled == qty:
            status = ChildStatus.FILLED
        elif filled > 0:
            status = ChildStatus.PARTIALLY_FILLED_CANCELED if terminal is not None else ChildStatus.PARTIALLY_FILLED
        elif terminal is not None:
            status = {ExecType.REJECTED: ChildStatus.REJECTED, ExecType.EXPIRED: ChildStatus.EXPIRED}.get(
                terminal.exec_type, ChildStatus.CANCELED)
        else:
            status = ChildStatus.UNFILLED_OPEN

        reports = [r for r in reports_by_child.get(key, []) if r.cum_qty is not None and r.exec_id]
        reported = None
        if reports:
            # Latest report by session sequence (assigned at send time) — robust to source clock drift;
            # timestamp only breaks ties across sessions.
            latest = max(reports, key=lambda r: (r.session_id or "", r.sequence_number if r.sequence_number is not None
                                                 else -1, r.execution_timestamp_ns, r.exec_id))
            reported = latest.cum_qty
            if reported != filled and not any(f.status is TradeStatus.AMBIGUOUS for f in fills):
                child_findings.append(Finding(ExceptionType.CUMQTY_MISMATCH,
                                              f"child {order.cl_ord_id}: broker CumQty {reported} vs reconciled {filled}",
                                              source_event_id=latest.source_event_id, entity_key=order.cl_ord_id))

        if order.intended_venue_id is not None:
            mismatched = sorted({f.actual_venue_id for f in active
                                 if f.mapping_status is MappingStatus.MAPPED and f.actual_venue_id != order.intended_venue_id})
            if mismatched:
                child_findings.append(Finding(ExceptionType.DESTINATION_MISMATCH,
                                              f"child {order.cl_ord_id} intended {order.intended_venue_id}, executed on "
                                              f"{', '.join(mismatched)}", source_event_id=order.event_id,
                                              entity_key=order.cl_ord_id))

        findings.extend(child_findings)
        children.append(
            ChildReconciliation(
                source_system=order.source_system,
                child_order_id=order.cl_ord_id,
                parent_order_id=order.parent_order_id,
                client_order_id=order.client_order_id,
                instrument_id=order.instrument_id,
                side=order.side,
                order_type=order.order_type,
                classification=order.classification,
                ordered_quantity=qty,
                filled_quantity=filled,
                status=status,
                intended_venue_id=order.intended_venue_id,
                intended_mapping_status=order.mapping_status,
                raw_ex_destination=order.raw_ex_destination,
                venue_breakdown=dict(breakdown),
                fill_count=len(active),
                reported_cum_qty=reported,
                order_event_id=order.event_id,
                arrival_time_ns=_ns(order),
                exception_types=[f.exception_type.value for f in child_findings],
            )
        )

    # -- parents ----------------------------------------------------------
    by_parent: dict[str, list[ChildReconciliation]] = defaultdict(list)
    for c in children:
        by_parent[c.parent_order_id or "UNLINKED"].append(c)
    parents = []
    for parent_id in sorted(by_parent):
        group = by_parent[parent_id]
        breakdown: dict[str, Decimal] = defaultdict(lambda: ZERO)
        statuses: dict[str, int] = defaultdict(int)
        for c in group:
            statuses[c.status.value] += 1
            for venue, q in c.venue_breakdown.items():
                breakdown[venue] += q
        parents.append(ParentReconciliation(
            parent_order_id=parent_id,
            child_order_ids=sorted(c.child_order_id for c in group),
            ordered_quantity=sum((c.ordered_quantity for c in group), ZERO),
            filled_quantity=sum((c.filled_quantity for c in group), ZERO),
            venue_breakdown=dict(breakdown),
            statuses=dict(statuses),
        ))

    active_all = [e for e in effective if e.status is TradeStatus.ACTIVE]
    active_qty = sum((e.quantity for e in active_all), ZERO)
    unresolved_qty = sum((e.quantity for e in active_all if e.venue_key == UNRESOLVED), ZERO)
    silent_fallbacks = sum(
        1 for e in active_all
        if e.raw_last_mkt in (None, "") and e.actual_venue_id is not None
    )
    complete = sum(1 for e in active_all if e.lineage_status == "COMPLETE")
    kpis = {
        "orders": len(order_index),
        "economic_reports": len(economic),
        "unique_exec_ids": len(unique),
        "duplicate_messages_excluded": duplicate_messages,
        "duplicate_quantity_excluded": str(duplicate_quantity),
        "conflicting_exec_ids": len(conflicted),
        "conflicting_quantity_excluded": str(conflicted_quantity),
        "trades_active": len(active_all),
        "trades_busted": sum(1 for e in effective if e.status is TradeStatus.BUSTED),
        "trades_ambiguous": sum(1 for e in effective if e.status is TradeStatus.AMBIGUOUS),
        "executed_quantity": str(active_qty),
        "attributed_quantity": str(active_qty - unresolved_qty),
        "unresolved_venue_quantity": str(unresolved_qty),
        "actual_venue_attribution_ratio": float((active_qty - unresolved_qty) / active_qty) if active_qty else 1.0,
        "lineage_complete_ratio": complete / len(active_all) if active_all else 1.0,
        "lineage_exceptions": len(active_all) - complete,
        "silent_venue_fallbacks": silent_fallbacks,
        "children_by_status": dict(sorted(_count(c.status.value for c in children).items())),
    }
    findings.sort(key=lambda f: f.dedup_key)
    return ReconciliationReport(children=children, parents=parents, executions=effective, findings=findings, kpis=kpis)


def _replace(e: EffectiveExecution, **changes) -> EffectiveExecution:
    from dataclasses import replace

    return replace(e, **changes)


def _ns(order: NormalizedOrder) -> int:
    from venue_integrity.timeutil import to_epoch_ns

    return to_epoch_ns(order.event_time)


def _count(values) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for v in values:
        out[v] += 1
    return out


def reconcile_order_executions(orders: list[dict], executions: list[dict]) -> list[dict]:
    """Minimal dict-based reconciliation matching the spec §6.2 reference sketch.

    Executions are de-duplicated by ExecID before summing, and the status
    vocabulary is FILLED / PARTIALLY_FILLED / UNFILLED / OVERFILLED.
    """
    seen: set[str] = set()
    grouped: dict[str, list[dict]] = defaultdict(list)
    for execution in executions:
        exec_id = execution.get("exec_id")
        if exec_id is not None:
            if exec_id in seen:
                continue
            seen.add(exec_id)
        grouped[execution["child_order_id"]].append(execution)
    results = []
    for order in orders:
        fills = grouped.get(order["child_order_id"], [])
        executed = sum((Decimal(str(f["executed_quantity"])) for f in fills), ZERO)
        ordered = Decimal(str(order["order_quantity"]))
        if executed > ordered:
            status = "OVERFILLED"
        elif executed == ordered:
            status = "FILLED"
        elif executed > 0:
            status = "PARTIALLY_FILLED"
        else:
            status = "UNFILLED"
        results.append({"child_order_id": order["child_order_id"], "ordered_quantity": ordered,
                        "executed_quantity": executed, "status": status,
                        "venues": sorted({f.get("actual_venue_id") or UNRESOLVED for f in fills})})
    return results
