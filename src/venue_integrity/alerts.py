"""Alert definitions (spec §13.2) and an in-process alert sink.

Alerts are notifications to operations teams. They never alter or block live
orders: the audit has no order-entry path (spec §13.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from venue_integrity.schemas.enums import ExceptionType, Severity


class AlertCode(StrEnum):
    VENUE_001 = "VENUE-001"
    VENUE_002 = "VENUE-002"
    VENUE_003 = "VENUE-003"
    VENUE_004 = "VENUE-004"
    VENUE_005 = "VENUE-005"
    VENUE_006 = "VENUE-006"
    VENUE_007 = "VENUE-007"
    VENUE_008 = "VENUE-008"


@dataclass(frozen=True)
class AlertDefinition:
    code: AlertCode
    trigger: str
    response: str


ALERT_DEFINITIONS: dict[AlertCode, AlertDefinition] = {
    AlertCode.VENUE_001: AlertDefinition(AlertCode.VENUE_001, "Unknown venue identifier", "Create reference-data incident"),
    AlertCode.VENUE_002: AlertDefinition(AlertCode.VENUE_002, "Market order becomes LEGACY", "Critical classification incident"),
    AlertCode.VENUE_003: AlertDefinition(AlertCode.VENUE_003, "Duplicate execution", "Stop duplicate economic inclusion"),
    AlertCode.VENUE_004: AlertDefinition(AlertCode.VENUE_004, "Execution destination mismatch", "Investigate routing lineage"),
    AlertCode.VENUE_005: AlertDefinition(AlertCode.VENUE_005, "Graph/TCA divergence", "Quantitative investigation"),
    AlertCode.VENUE_006: AlertDefinition(AlertCode.VENUE_006, "SOR feature missing", "Apply approved safe fallback"),
    AlertCode.VENUE_007: AlertDefinition(AlertCode.VENUE_007, "Mapping version changes", "Revalidate and retain audit trail"),
    AlertCode.VENUE_008: AlertDefinition(AlertCode.VENUE_008, "Data-feed sequence gap", "Recover and reconcile session"),
}

ALERT_BY_EXCEPTION: dict[ExceptionType, AlertCode] = {
    ExceptionType.UNKNOWN_DESTINATION: AlertCode.VENUE_001,
    ExceptionType.AMBIGUOUS_VENUE: AlertCode.VENUE_001,
    ExceptionType.INVALID_VENUE: AlertCode.VENUE_001,
    ExceptionType.UNRESOLVED_EXECUTION_VENUE: AlertCode.VENUE_001,
    ExceptionType.MARKET_ORDER_LEGACY_CLASSIFICATION: AlertCode.VENUE_002,
    ExceptionType.DUPLICATE_EXEC_ID: AlertCode.VENUE_003,
    ExceptionType.CONFLICTING_EXEC_ID: AlertCode.VENUE_003,
    ExceptionType.DESTINATION_MISMATCH: AlertCode.VENUE_004,
    ExceptionType.GRAPH_TCA_DIVERGENCE: AlertCode.VENUE_005,
    ExceptionType.ROUTER_FEATURE_DIVERGENCE: AlertCode.VENUE_005,
    ExceptionType.MISSING_MODEL_FEATURE: AlertCode.VENUE_006,
    ExceptionType.MAPPING_VERSION_CHANGE: AlertCode.VENUE_007,
    ExceptionType.SEQUENCE_GAP: AlertCode.VENUE_008,
}


@dataclass(frozen=True)
class Alert:
    code: AlertCode
    severity: Severity
    raised_at: datetime
    exception_id: str | None
    summary: str
    context: dict[str, Any] = field(default_factory=dict)


class AlertSink:
    """Collects alerts. Production deployments forward these to the paging system;
    the Prometheus exporter (api/monitoring.py) exposes counts per code."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def emit(self, alert: Alert) -> None:
        self.alerts.append(alert)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {code.value: 0 for code in AlertCode}
        for alert in self.alerts:
            counts[alert.code.value] += 1
        return counts
