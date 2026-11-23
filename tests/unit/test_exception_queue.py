from datetime import UTC, datetime

import pytest

from venue_integrity.alerts import AlertCode, AlertSink
from venue_integrity.identity.exception_queue import ExceptionQueue, ExceptionTransitionError, Finding
from venue_integrity.schemas.enums import DefectCategory, ExceptionStatus, ExceptionType, Severity

AT = datetime(2026, 11, 23, tzinfo=UTC)


def test_severity_table_and_alerts():
    sink = AlertSink()
    q = ExceptionQueue(sink)
    expected = {
        ExceptionType.UNKNOWN_DESTINATION: Severity.HIGH,
        ExceptionType.AMBIGUOUS_VENUE: Severity.CRITICAL,
        ExceptionType.MISSING_ORDER_TYPE: Severity.HIGH,
        ExceptionType.DUPLICATE_EXEC_ID: Severity.CRITICAL,
        ExceptionType.INVALID_TIMESTAMP: Severity.HIGH,
        ExceptionType.UNKNOWN_FIX_TAG: Severity.MEDIUM,
        ExceptionType.SEQUENCE_GAP: Severity.HIGH,
    }
    for i, (t, sev) in enumerate(expected.items()):
        assert q.raise_finding(Finding(t, "x", source_event_id=str(i)), AT).severity is sev
    counts = sink.counts()
    assert counts[AlertCode.VENUE_001.value] == 2  # unknown + ambiguous
    assert counts[AlertCode.VENUE_003.value] == 1 and counts[AlertCode.VENUE_008.value] == 1


def test_deduplication_counts_occurrences():
    q = ExceptionQueue()
    f = Finding(ExceptionType.UNKNOWN_DESTINATION, "x", source_event_id="e1", raw_value="ABC_ROUTER")
    a = q.raise_finding(f, AT)
    b = q.raise_finding(f, AT)
    assert a is b and a.occurrences == 2 and len(q) == 1


def test_lifecycle_requires_evidence_and_valid_transitions():
    q = ExceptionQueue()
    rec = q.raise_finding(Finding(ExceptionType.UNKNOWN_DESTINATION, "x", source_event_id="e1"), AT)
    with pytest.raises(ExceptionTransitionError):
        q.mark_reprocessed(rec.exception_id, "x", AT, "sys")
    with pytest.raises(ExceptionTransitionError):
        q.resolve(rec.exception_id, DefectCategory.VENUE_REFERENCE_DATA, "  ", AT, "me")
    q.assign(rec.exception_id, "alice", AT, "lead")
    q.resolve(rec.exception_id, DefectCategory.VENUE_REFERENCE_DATA, "change request CR-1", AT, "alice")
    q.mark_reprocessed(rec.exception_id, "now MAPPED to VENUE_C", AT, "system")
    q.close(rec.exception_id, AT, "lead")
    assert rec.status is ExceptionStatus.CLOSED
    assert [h["action"] for h in rec.history] == ["OPENED", "ASSIGNED", "RESOLVED", "REPROCESSED", "CLOSED"]
    with pytest.raises(ExceptionTransitionError):
        q.assign(rec.exception_id, "bob", AT, "lead")
    assert q.unresolved() == []


def test_summary_and_filters():
    q = ExceptionQueue()
    q.raise_finding(Finding(ExceptionType.UNKNOWN_FIX_TAG, "x", source_event_id="1"), AT)
    q.raise_finding(Finding(ExceptionType.OVERFILL, "x", source_event_id="2"), AT)
    assert q.summary()["by_severity"] == {"CRITICAL": 1, "MEDIUM": 1}
    assert len(q.filter(severity=Severity.CRITICAL)) == 1
    assert q.records()[0].to_dict()["status"] == "OPEN"
