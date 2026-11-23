"""Stateful audit service behind the API.

Holds one :class:`AuditPipeline` (raw store, registry, exception queue) and
caches derived views until new events arrive or reference data changes.
Optionally bootstraps the synthetic scenario so analytics endpoints have data.
"""

from __future__ import annotations

import threading
from typing import Any

from venue_integrity.execution.order_normalizer import classify_order, normalize_order_type
from venue_integrity.identity.mapping_history import MappingChangeManager
from venue_integrity.ingestion.event_validator import validate_message
from venue_integrity.ingestion.fix_parser import parse_fix
from venue_integrity.ingestion.raw_event_store import AppendOutcome, RawEventStore, build_raw_event
from venue_integrity.pipeline import AuditPipeline
from venue_integrity.schemas.enums import ExceptionStatus, MappingStatus
from venue_integrity.schemas.raw_event import RawCapture


class AuditService:
    def __init__(self, store_path: str = ":memory:", bootstrap: str | None = None):
        self.lock = threading.RLock()
        self.pipeline = AuditPipeline(raw_store=RawEventStore(store_path), mode="replay")
        self.changes = MappingChangeManager(self.pipeline.registry, self.pipeline.mic_reference)
        self.market_data = None
        self.decisions: dict[str, Any] = {}
        self._cache: dict[str, Any] = {}
        if bootstrap == "simulation":
            self.bootstrap_simulation()

    # -- state -------------------------------------------------------------
    def invalidate(self) -> None:
        self._cache.clear()

    @property
    def registry(self):
        return self.changes.registry

    def bootstrap_simulation(self) -> None:
        from venue_integrity.simulation.generator import generate_scenario

        bundle = generate_scenario()
        with self.lock:
            self.pipeline.ingest(bundle.captures)
            self.market_data = bundle.market_data
            self.decisions = {d.decision_id: d for d in bundle.decisions}
            self.invalidate()

    def ingest(self, capture: RawCapture) -> dict[str, Any]:
        with self.lock:
            before = self.pipeline.ingestion.stored
            report = self.pipeline.ingest([capture])
            event = build_raw_event(capture)
            stored = report.stored > before
            message = parse_fix(event.raw_payload)
            validation = validate_message(message, self.pipeline.raw_store.get(event.event_id), self.pipeline.dictionary)
            self.invalidate()
            return {
                "event_id": event.event_id,
                "outcome": AppendOutcome.STORED.value if stored else AppendOutcome.DUPLICATE_DELIVERY.value,
                "payload_hash": event.payload_hash,
                "validation_status": validation.status.value,
                "findings": [f.exception_type.value for f in validation.findings],
            }

    def normalized(self):
        with self.lock:
            if "normalized" not in self._cache:
                self._cache["normalized"] = self.pipeline.normalize(registry=self.registry)
            return self._cache["normalized"]

    def reconciliation(self):
        with self.lock:
            if "reconciliation" not in self._cache:
                self._cache["reconciliation"] = self.pipeline.reconcile(self.normalized())
            return self._cache["reconciliation"]

    def tca(self):
        if self.market_data is None:
            return None
        with self.lock:
            if "tca" not in self._cache:
                self._cache["tca"] = self.pipeline.run_tca(self.market_data, self.reconciliation())
            return self._cache["tca"]

    def graph(self):
        if self.market_data is None:
            return None
        with self.lock:
            if "graph" not in self._cache:
                self._cache["graph"] = self.pipeline.run_graph(self.market_data)
            return self._cache["graph"]

    def refresh_metrics(self, metrics) -> None:
        with self.lock:
            n = self.normalized()
            rec = self.reconciliation()
            metrics.unresolved.clear()
            for r in self.pipeline.exceptions.unresolved():
                metrics.unresolved.labels(r.exception_type.value, r.severity.value).inc()
            for code, count in self.pipeline.alerts.counts().items():
                metrics.alerts.labels(code).set(count)
            ids = [o.mapping_status for o in n.orders if o.raw_ex_destination]
            ids += [e.mapping_status for e in n.executions if e.exec_type.is_economic]
            metrics.mapping_coverage.set(sum(1 for s in ids if s is MappingStatus.MAPPED) / len(ids) if ids else 1.0)
            metrics.unmapped_volume.set(float(sum(o.order_quantity for o in n.orders
                                                  if o.raw_ex_destination and o.mapping_status is not MappingStatus.MAPPED)))
            metrics.silent_coercions.set(sum(1 for o in n.orders
                                             if o.classification is not classify_order(normalize_order_type(o.raw_ord_type))))
            metrics.silent_fallbacks.set(rec.kpis["silent_venue_fallbacks"])
            metrics.lineage_ratio.set(rec.kpis["lineage_complete_ratio"])
            metrics.attribution_ratio.set(rec.kpis["actual_venue_attribution_ratio"])
            metrics.sequence_gaps.set(sum(len(v) for v in self.pipeline.sequence_monitor.open_gaps().values()))
            metrics.registry_version.clear()
            metrics.registry_version.labels(self.registry.latest_version).set(1)
            metrics.market_legacy.clear()
            for o in n.orders:
                if o.raw_ord_type == "1":
                    metrics.market_legacy.labels(o.classification.value).inc()

    def exceptions(self, *, status: str | None, severity: str | None, exception_type: str | None, limit: int) -> list[dict]:
        records = self.pipeline.exceptions.records()
        if status:
            records = [r for r in records if r.status.value == status]
        if severity:
            records = [r for r in records if r.severity.value == severity]
        if exception_type:
            records = [r for r in records if r.exception_type.value == exception_type]
        return [r.to_dict() for r in records[:limit]]

    def open_count(self) -> int:
        return len([r for r in self.pipeline.exceptions.records()
                    if r.status in (ExceptionStatus.OPEN, ExceptionStatus.ASSIGNED)])
