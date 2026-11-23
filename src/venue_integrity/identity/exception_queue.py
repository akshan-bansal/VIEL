"""Exception-management queue (spec §5.3).

Every unresolved or suspicious record becomes an exception with an owner, a
status, a root-cause classification, resolution evidence and a reprocessing
outcome. History is append-only: each transition is recorded, never rewritten.

Modules do not touch the queue directly; they return :class:`Finding` objects
which the pipeline raises. This keeps parsing, resolution and reconciliation
pure and testable.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from venue_integrity.alerts import ALERT_BY_EXCEPTION, Alert, AlertCode, AlertSink
from venue_integrity.schemas.common import VIA_NAMESPACE
from venue_integrity.schemas.enums import (
    DefectCategory,
    ExceptionStatus,
    ExceptionType,
    Severity,
)
from venue_integrity.timeutil import isoformat

SEVERITY_BY_TYPE: dict[ExceptionType, Severity] = {
    # spec §5.3 table
    ExceptionType.UNKNOWN_DESTINATION: Severity.HIGH,
    ExceptionType.AMBIGUOUS_VENUE: Severity.CRITICAL,
    ExceptionType.MISSING_ORDER_TYPE: Severity.HIGH,
    ExceptionType.DUPLICATE_EXEC_ID: Severity.CRITICAL,
    ExceptionType.INVALID_TIMESTAMP: Severity.HIGH,
    ExceptionType.UNKNOWN_FIX_TAG: Severity.MEDIUM,
    ExceptionType.SEQUENCE_GAP: Severity.HIGH,
    # extensions
    ExceptionType.INVALID_VENUE: Severity.HIGH,
    ExceptionType.UNRESOLVED_EXECUTION_VENUE: Severity.HIGH,
    ExceptionType.PROTOCOL_VALIDATION_FAILURE: Severity.HIGH,
    ExceptionType.UNKNOWN_ENUM_VALUE: Severity.HIGH,
    ExceptionType.UNKNOWN_ORDER_TYPE_CODE: Severity.HIGH,
    ExceptionType.INVALID_SIDE: Severity.HIGH,
    ExceptionType.CLOCK_DRIFT: Severity.HIGH,
    ExceptionType.SEQUENCE_DUPLICATE: Severity.MEDIUM,
    ExceptionType.CONFLICTING_EXEC_ID: Severity.CRITICAL,
    ExceptionType.ORPHAN_EXECUTION: Severity.HIGH,
    ExceptionType.ORPHAN_CORRECTION: Severity.HIGH,
    ExceptionType.AMBIGUOUS_CORRECTION: Severity.HIGH,
    ExceptionType.OVERFILL: Severity.CRITICAL,
    ExceptionType.CUMQTY_MISMATCH: Severity.HIGH,
    ExceptionType.MISSING_LINEAGE: Severity.HIGH,
    ExceptionType.FILL_AFTER_TERMINAL_STATE: Severity.HIGH,
    ExceptionType.UNSUPPORTED_ORDER_EVENT: Severity.MEDIUM,
    ExceptionType.DESTINATION_MISMATCH: Severity.MEDIUM,
    ExceptionType.MARKET_ORDER_LEGACY_CLASSIFICATION: Severity.CRITICAL,
    ExceptionType.STALE_MIDPOINT: Severity.MEDIUM,
    ExceptionType.GRAPH_TCA_DIVERGENCE: Severity.MEDIUM,
    ExceptionType.MISSING_MODEL_FEATURE: Severity.HIGH,
    ExceptionType.DECISION_NOT_REPRODUCIBLE: Severity.HIGH,
    ExceptionType.LOOKAHEAD_FEATURE: Severity.CRITICAL,
    ExceptionType.ROUTER_FEATURE_DIVERGENCE: Severity.HIGH,
    ExceptionType.MAPPING_VERSION_CHANGE: Severity.INFO,
}

REQUIRED_ACTION: dict[ExceptionType, str] = {
    ExceptionType.UNKNOWN_DESTINATION: "Resolve venue alias",
    ExceptionType.AMBIGUOUS_VENUE: "Reference-data investigation",
    ExceptionType.MISSING_ORDER_TYPE: "Protocol validation",
    ExceptionType.DUPLICATE_EXEC_ID: "Reconciliation",
    ExceptionType.INVALID_TIMESTAMP: "Clock/data-quality investigation",
    ExceptionType.UNKNOWN_FIX_TAG: "Dictionary compatibility review",
    ExceptionType.SEQUENCE_GAP: "Session recovery",
}


@dataclass(frozen=True)
class Finding:
    """An exception candidate produced by a pure module."""

    exception_type: ExceptionType
    detail: str
    source_event_id: str | None = None
    entity_key: str | None = None
    raw_value: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    severity: Severity | None = None

    @property
    def dedup_key(self) -> str:
        return "|".join(
            [
                self.exception_type.value,
                self.source_event_id or "",
                self.entity_key or "",
                self.raw_value or "",
            ]
        )


_ALLOWED_TRANSITIONS: dict[ExceptionStatus, set[ExceptionStatus]] = {
    ExceptionStatus.OPEN: {ExceptionStatus.ASSIGNED, ExceptionStatus.RESOLVED, ExceptionStatus.WAIVED},
    ExceptionStatus.ASSIGNED: {ExceptionStatus.ASSIGNED, ExceptionStatus.RESOLVED, ExceptionStatus.WAIVED},
    ExceptionStatus.RESOLVED: {ExceptionStatus.REPROCESSED, ExceptionStatus.OPEN},
    ExceptionStatus.REPROCESSED: {ExceptionStatus.CLOSED, ExceptionStatus.OPEN},
    ExceptionStatus.CLOSED: set(),
    ExceptionStatus.WAIVED: set(),
}


class ExceptionTransitionError(ValueError):
    pass


@dataclass
class ExceptionRecord:
    exception_id: str
    exception_type: ExceptionType
    severity: Severity
    alert_code: AlertCode | None
    detail: str
    source_event_id: str | None
    entity_key: str | None
    raw_value: str | None
    context: dict[str, Any]
    created_at: datetime
    status: ExceptionStatus = ExceptionStatus.OPEN
    owner: str | None = None
    root_cause: DefectCategory | None = None
    resolution_evidence: str | None = None
    reprocessing_outcome: str | None = None
    occurrences: int = 1
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def required_action(self) -> str | None:
        return REQUIRED_ACTION.get(self.exception_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "exception_id": self.exception_id,
            "exception_type": self.exception_type.value,
            "severity": self.severity.value,
            "alert_code": self.alert_code.value if self.alert_code else None,
            "detail": self.detail,
            "source_event_id": self.source_event_id,
            "entity_key": self.entity_key,
            "raw_value": self.raw_value,
            "context": _jsonable(self.context),
            "created_at": isoformat(self.created_at),
            "status": self.status.value,
            "owner": self.owner,
            "root_cause": self.root_cause.value if self.root_cause else None,
            "resolution_evidence": self.resolution_evidence,
            "reprocessing_outcome": self.reprocessing_outcome,
            "occurrences": self.occurrences,
            "required_action": self.required_action,
            "history": [_jsonable(h) for h in self.history],
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return isoformat(value)
    if hasattr(value, "value") and not isinstance(value, (int, float, str)):
        return value.value
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


class ExceptionQueue:
    def __init__(self, alert_sink: AlertSink | None = None):
        self._records: dict[str, ExceptionRecord] = {}
        self.alert_sink = alert_sink

    def raise_finding(self, finding: Finding, at: datetime) -> ExceptionRecord:
        exception_id = str(uuid.uuid5(VIA_NAMESPACE, "exception|" + finding.dedup_key))
        existing = self._records.get(exception_id)
        if existing is not None:
            existing.occurrences += 1
            existing.history.append({"at": at, "action": "RECURRED", "by": "system"})
            return existing
        severity = finding.severity or SEVERITY_BY_TYPE[finding.exception_type]
        alert_code = ALERT_BY_EXCEPTION.get(finding.exception_type)
        record = ExceptionRecord(
            exception_id=exception_id,
            exception_type=finding.exception_type,
            severity=severity,
            alert_code=alert_code,
            detail=finding.detail,
            source_event_id=finding.source_event_id,
            entity_key=finding.entity_key,
            raw_value=finding.raw_value,
            context=dict(finding.context),
            created_at=at,
        )
        record.history.append({"at": at, "action": "OPENED", "by": "system"})
        self._records[exception_id] = record
        if alert_code is not None and self.alert_sink is not None:
            self.alert_sink.emit(
                Alert(
                    code=alert_code,
                    severity=severity,
                    raised_at=at,
                    exception_id=exception_id,
                    summary=f"{finding.exception_type.value}: {finding.detail}",
                    context={"raw_value": finding.raw_value, "entity_key": finding.entity_key},
                )
            )
        return record

    def raise_all(self, findings, at: datetime) -> list[ExceptionRecord]:
        return [self.raise_finding(f, at) for f in findings]

    def get(self, exception_id: str) -> ExceptionRecord:
        return self._records[exception_id]

    def _transition(self, record: ExceptionRecord, new: ExceptionStatus, at: datetime, by: str, **extra) -> None:
        if new not in _ALLOWED_TRANSITIONS[record.status]:
            raise ExceptionTransitionError(f"{record.status} -> {new} not allowed")
        record.history.append({"at": at, "action": new.value, "by": by, "from": record.status.value, **extra})
        record.status = new

    def assign(self, exception_id: str, owner: str, at: datetime, by: str) -> ExceptionRecord:
        record = self._records[exception_id]
        record.owner = owner
        self._transition(record, ExceptionStatus.ASSIGNED, at, by, owner=owner)
        return record

    def resolve(
        self, exception_id: str, root_cause: DefectCategory, evidence: str, at: datetime, by: str
    ) -> ExceptionRecord:
        if not evidence or not evidence.strip():
            raise ExceptionTransitionError("resolution requires evidence")
        record = self._records[exception_id]
        record.root_cause = root_cause
        record.resolution_evidence = evidence
        self._transition(record, ExceptionStatus.RESOLVED, at, by, root_cause=root_cause.value, evidence=evidence)
        return record

    def mark_reprocessed(self, exception_id: str, outcome: str, at: datetime, by: str) -> ExceptionRecord:
        record = self._records[exception_id]
        record.reprocessing_outcome = outcome
        self._transition(record, ExceptionStatus.REPROCESSED, at, by, outcome=outcome)
        return record

    def close(self, exception_id: str, at: datetime, by: str) -> ExceptionRecord:
        record = self._records[exception_id]
        self._transition(record, ExceptionStatus.CLOSED, at, by)
        return record

    def waive(self, exception_id: str, reason: str, at: datetime, approver: str) -> ExceptionRecord:
        if not reason.strip():
            raise ExceptionTransitionError("waiver requires a reason")
        record = self._records[exception_id]
        self._transition(record, ExceptionStatus.WAIVED, at, approver, reason=reason)
        return record

    def records(self) -> list[ExceptionRecord]:
        return sorted(self._records.values(), key=lambda r: (r.created_at, r.exception_id))

    def unresolved(self) -> list[ExceptionRecord]:
        return [
            r for r in self.records() if r.status in (ExceptionStatus.OPEN, ExceptionStatus.ASSIGNED)
        ]

    def filter(self, *, exception_type=None, status=None, severity=None) -> list[ExceptionRecord]:
        out = self.records()
        if exception_type is not None:
            out = [r for r in out if r.exception_type == exception_type]
        if status is not None:
            out = [r for r in out if r.status == status]
        if severity is not None:
            out = [r for r in out if r.severity == severity]
        return out

    def summary(self) -> dict[str, Any]:
        by_type = Counter(r.exception_type.value for r in self._records.values())
        by_severity = Counter(r.severity.value for r in self._records.values())
        by_status = Counter(r.status.value for r in self._records.values())
        return {
            "total": len(self._records),
            "unresolved": len(self.unresolved()),
            "by_type": dict(sorted(by_type.items())),
            "by_severity": dict(sorted(by_severity.items())),
            "by_status": dict(sorted(by_status.items())),
        }

    def __len__(self) -> int:
        return len(self._records)
