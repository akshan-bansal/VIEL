"""Execution lineage: client order -> parent -> child -> execution -> raw message (spec §6.1).

``LineageIndex.trace`` walks an execution back to the raw source messages and
re-verifies each payload hash against the append-only store, so a lineage
record is evidence, not just a join.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from venue_integrity.execution.execution_reconciler import ReconciliationReport
from venue_integrity.ingestion.raw_event_store import RawEventStore
from venue_integrity.schemas.common import sha256_hex
from venue_integrity.schemas.normalized_order import NormalizedOrder


@dataclass(frozen=True)
class LineageTrace:
    exec_id: str
    client_order_id: str | None
    parent_order_id: str | None
    child_order_id: str | None
    order_event_id: str | None
    execution_event_ids: tuple[str, ...]
    payload_hashes_verified: bool
    complete: bool
    gaps: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "exec_id": self.exec_id,
            "client_order_id": self.client_order_id,
            "parent_order_id": self.parent_order_id,
            "child_order_id": self.child_order_id,
            "order_event_id": self.order_event_id,
            "execution_event_ids": list(self.execution_event_ids),
            "payload_hashes_verified": self.payload_hashes_verified,
            "complete": self.complete,
            "gaps": list(self.gaps),
        }


class LineageIndex:
    def __init__(self, orders: list[NormalizedOrder], report: ReconciliationReport, store: RawEventStore | None = None):
        self.store = store
        self.orders = {(o.source_system, o.cl_ord_id): o for o in sorted(orders, key=lambda o: o.event_id, reverse=True)}
        self.report = report
        self.executions = {(e.source_system, e.original_exec_id): e for e in report.executions}

    def _verify(self, event_ids) -> bool:
        if self.store is None:
            return False
        for event_id in event_ids:
            event = self.store.get(event_id)  # RawEvent construction re-verifies the hash
            if sha256_hex(event.raw_payload) != event.payload_hash:  # pragma: no cover - defensive
                return False
        return True

    def trace(self, exec_id: str, source_system: str) -> LineageTrace:
        execution = self.executions.get((source_system, exec_id))
        if execution is None:
            raise KeyError(exec_id)
        order = self.orders.get((source_system, execution.child_order_id or ""))
        gaps = []
        if order is None:
            gaps.append("no child order for execution")
        parent = execution.parent_order_id or (order.parent_order_id if order else None)
        if parent is None:
            gaps.append("no parent order linkage")
        client = order.client_order_id if order else None
        if client is None:
            gaps.append("no client order linkage")
        event_ids = list(execution.source_event_ids)
        if order is not None:
            event_ids.append(order.event_id)
        verified = self._verify(event_ids)
        if not verified:
            gaps.append("raw payloads not verified")
        return LineageTrace(
            exec_id=exec_id,
            client_order_id=client,
            parent_order_id=parent,
            child_order_id=execution.child_order_id,
            order_event_id=order.event_id if order else None,
            execution_event_ids=tuple(execution.source_event_ids),
            payload_hashes_verified=verified,
            complete=not gaps,
            gaps=tuple(gaps),
        )

    def tree(self, parent_order_id: str) -> dict[str, Any]:
        children = [c for c in self.report.children if c.parent_order_id == parent_order_id]
        client_ids = sorted({c.client_order_id for c in children if c.client_order_id})
        fills = defaultdict(list)
        for e in self.report.executions:
            if e.parent_order_id == parent_order_id:
                fills[e.child_order_id].append(e)
        return {
            "client_order_ids": client_ids,
            "parent_order_id": parent_order_id,
            "children": [
                {
                    "child_order_id": c.child_order_id,
                    "order_type": c.order_type.value,
                    "classification": c.classification.value,
                    "intended_venue_id": c.intended_venue_id,
                    "intended_mapping_status": c.intended_mapping_status.value,
                    "raw_ex_destination": c.raw_ex_destination,
                    "ordered_quantity": str(c.ordered_quantity),
                    "filled_quantity": str(c.filled_quantity),
                    "status": c.status.value,
                    "executions": [
                        {"exec_id": e.original_exec_id, "effective_exec_id": e.effective_exec_id,
                         "status": e.status.value, "quantity": str(e.quantity),
                         "price": str(e.price) if e.price is not None else None,
                         "actual_venue": e.venue_key, "raw_last_mkt": e.raw_last_mkt}
                        for e in sorted(fills.get(c.child_order_id, []), key=lambda x: x.trade_timestamp_ns)
                    ],
                }
                for c in sorted(children, key=lambda c: c.child_order_id)
            ],
        }

    @staticmethod
    def render_tree(tree: dict[str, Any]) -> str:
        lines = [f"Client order {', '.join(tree['client_order_ids']) or '(none)'}",
                 "    |", f"    +-- Parent order {tree['parent_order_id']}"]
        for i, child in enumerate(tree["children"]):
            last_child = i == len(tree["children"]) - 1
            branch = "            " if last_child else "            |"
            lines.append("            |")
            lines.append(
                f"            +-- Child {child['child_order_id']} [{child['classification']}] "
                f"intended={child['intended_venue_id'] or child['raw_ex_destination'] + ' (' + child['intended_mapping_status'] + ')'} "
                f"filled {child['filled_quantity']}/{child['ordered_quantity']} {child['status']}"
            )
            for execution in child["executions"]:
                lines.append(
                    f"{branch}       +-- Execution {execution['exec_id']} {execution['status']} "
                    f"{execution['quantity']} @ {execution['price']} on {execution['actual_venue']}"
                    + (f" (LastMkt={execution['raw_last_mkt']})" if execution["raw_last_mkt"] else " (LastMkt missing)")
                )
        return "\n".join(lines)
