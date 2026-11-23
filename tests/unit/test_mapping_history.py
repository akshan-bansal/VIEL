from datetime import UTC, datetime

import pytest

from venue_integrity.identity.mapping_history import MakerCheckerError, MappingChangeManager
from venue_integrity.identity.venue_resolver import resolve_venue
from venue_integrity.schemas.enums import ExceptionType, MappingStatus

AT = datetime(2026, 11, 21, tzinfo=UTC)
NOV = datetime(2026, 11, 10, 14, 30, tzinfo=UTC)
RECORD = {"venue_id": "VENUE_C", "mic": "XABC", "broker_aliases": {"BROKER_A": ["ABC_ROUTER"]},
          "venue_type": "EXCHANGE", "country": "CA", "valid_from": "2026-11-04T00:00:00Z"}


def test_maker_checker_and_new_version(registry, mic_reference):
    mgr = MappingChangeManager(registry, mic_reference)
    req = mgr.propose(proposed_by="maker", description="add", evidence="broker notes", add_records=[RECORD], at=AT)
    with pytest.raises(MakerCheckerError):
        mgr.approve(req.request_id, approved_by="maker", new_version="2.1", at=AT)
    new, finding = mgr.approve(req.request_id, approved_by="checker", new_version="2.1", at=AT)
    assert finding.exception_type is ExceptionType.MAPPING_VERSION_CHANGE
    assert [v.version for v in new.versions] == ["2.0", "2.1"]
    after = resolve_venue("ABC_ROUTER", new, event_time=NOV, source_system="BROKER_A")
    assert after.venue_id == "VENUE_C" and after.mapping_version == "2.1"
    # history is replayable exactly
    assert resolve_venue("ABC_ROUTER", new, "2.0", event_time=NOV, source_system="BROKER_A").mapping_status is MappingStatus.UNMAPPED
    # business-time validity is honoured
    assert resolve_venue("ABC_ROUTER", new, event_time=datetime(2026, 11, 1, tzinfo=UTC),
                         source_system="BROKER_A").mapping_status is MappingStatus.UNMAPPED
    # the original registry object is untouched
    assert registry.latest_version == "2.0"
    with pytest.raises(ValueError):
        mgr.approve(req.request_id, approved_by="checker", new_version="2.2", at=AT)


def test_supersede_resolves_ambiguity(registry, mic_reference):
    mgr = MappingChangeManager(registry, mic_reference)
    bad = next(r for r in registry.records_at() if r.venue_id == "VENUE_D" and "SMART" in r.broker_aliases.get("BROKER_B", ()))
    req = mgr.propose(proposed_by="maker", description="withdraw", evidence="broker confirmation", supersede=[bad], at=AT)
    new, _ = mgr.approve(req.request_id, approved_by="checker", new_version="2.1", at=AT)
    assert resolve_venue("SMART", new, event_time=NOV, source_system="BROKER_B").venue_id == "VENUE_A"
    assert resolve_venue("SMART", new, "2.0", event_time=NOV, source_system="BROKER_B").mapping_status is MappingStatus.AMBIGUOUS


def test_proposals_are_validated(registry, mic_reference):
    mgr = MappingChangeManager(registry, mic_reference)
    with pytest.raises(ValueError, match="evidence"):
        mgr.propose(proposed_by="m", description="d", evidence=" ", add_records=[RECORD], at=AT)
    with pytest.raises(ValueError, match="authoritative reference"):
        mgr.propose(proposed_by="m", description="d", evidence="e", add_records=[{**RECORD, "mic": "XVNX"}], at=AT)
    with pytest.raises(ValueError, match="empty"):
        mgr.propose(proposed_by="m", description="d", evidence="e", at=AT)
    req = mgr.propose(proposed_by="m", description="d", evidence="e", add_records=[RECORD], at=AT)
    mgr.reject(req.request_id, rejected_by="c", reason="insufficient evidence", at=AT)
    assert mgr.get(req.request_id).status.value == "REJECTED"


def test_snapshot_is_content_hashed(registry):
    a, b = registry.snapshot(), registry.snapshot()
    assert a["snapshot_hash"] == b["snapshot_hash"] and a["record_count"] == len(registry.records_at())
