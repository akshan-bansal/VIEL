from datetime import UTC, datetime

import pytest

from venue_integrity.identity.mic_registry import VenueRegistry
from venue_integrity.identity.venue_resolver import resolution_finding, resolve_venue
from venue_integrity.schemas.enums import ExceptionType, IdentifierType, MappingStatus

NOV = datetime(2026, 11, 10, 14, 30, tzinfo=UTC)
MAR = datetime(2026, 3, 10, 14, 30, tzinfo=UTC)


def r(raw, registry, mic_reference, **kw):
    kw.setdefault("event_time", NOV)
    return resolve_venue(raw, registry, mic_reference=mic_reference, **kw)


def test_global_alias(registry, mic_reference):
    res = r("ABC", registry, mic_reference)
    assert (res.mapping_status, res.venue_id, res.mic, res.match_type) == (MappingStatus.MAPPED, "VENUE_C", "XABC", "ALIAS")


@pytest.mark.parametrize("raw", ["abc", "ABC ", " ABC", "Abc", "ABC\n", "ABC_ROUTER", "XABC"])
def test_exact_match_only(registry, mic_reference, raw):
    res = r(raw, registry, mic_reference)
    assert res.mapping_status is MappingStatus.UNMAPPED and res.venue_id is None


@pytest.mark.parametrize("raw", [None, ""])
def test_missing_identifier(registry, mic_reference, raw):
    res = r(raw, registry, mic_reference)
    assert res.mapping_status is MappingStatus.UNMAPPED and res.reason == "MISSING_IDENTIFIER"


def test_broker_alias_is_broker_scoped(registry, mic_reference):
    assert r("B_ROUTE_7", registry, mic_reference, source_system="BROKER_B").venue_id == "VENUE_B"
    assert r("B_ROUTE_7", registry, mic_reference, source_system="BROKER_A").mapping_status is MappingStatus.UNMAPPED
    assert r("B_ROUTE_7", registry, mic_reference).mapping_status is MappingStatus.UNMAPPED


def test_ambiguous_alias_never_picks(registry, mic_reference):
    res = r("SMART", registry, mic_reference, source_system="BROKER_B")
    assert res.mapping_status is MappingStatus.AMBIGUOUS
    assert res.venue_id is None
    assert {c.venue_id for c in res.candidates} == {"VENUE_A", "VENUE_D"}


def test_effective_dating(registry, mic_reference):
    assert r("VNB_OLD", registry, mic_reference, event_time=MAR).venue_id == "VENUE_B"
    late = r("VNB_OLD", registry, mic_reference, event_time=NOV)
    assert late.mapping_status is MappingStatus.UNMAPPED
    assert late.reason == "IDENTIFIER_NOT_EFFECTIVE_AT_EVENT_TIME"
    assert late.candidates  # retained for investigation
    assert r("VNB", registry, mic_reference, event_time=MAR).venue_id == "VENUE_B"
    assert r("VNB", registry, mic_reference, event_time=NOV).venue_id == "VENUE_B"


def test_mic_identifier(registry, mic_reference):
    res = r("XABC", registry, mic_reference, identifier_type=IdentifierType.MIC)
    assert res.venue_id == "VENUE_C" and res.match_type == "MIC"
    assert r("ABC", registry, mic_reference, identifier_type=IdentifierType.MIC).mapping_status is MappingStatus.UNMAPPED


def test_mic_failing_authoritative_reference_is_invalid(registry, mic_reference):
    res = r("XVNX", registry, mic_reference, identifier_type=IdentifierType.MIC)
    assert res.mapping_status is MappingStatus.INVALID
    assert res.venue_id is None
    assert res.reason == "MIC_STATUS_EXPIRED"
    # without the authoritative reference the stale registry would have said MAPPED
    assert resolve_venue("XVNX", registry, event_time=NOV, identifier_type=IdentifierType.MIC).venue_id == "VENUE_X"


def test_unknown_registry_version(registry, mic_reference):
    res = r("ABC", registry, mic_reference, mapping_version="9.9")
    assert res.mapping_status is MappingStatus.INVALID and res.reason == "UNKNOWN_REGISTRY_VERSION"


def test_no_event_time_only_time_independent_records(registry):
    assert resolve_venue("ABC", registry).mapping_status is MappingStatus.UNMAPPED
    simple = VenueRegistry.from_alias_map({"ABC": [{"venue_id": "VENUE_A", "mic": "XABC"}]})
    assert resolve_venue("ABC", simple).venue_id == "VENUE_A"


def test_findings(registry, mic_reference):
    assert resolution_finding(r("ABC", registry, mic_reference), source_event_id="e", field="f") is None
    f = resolution_finding(r("NOPE", registry, mic_reference), source_event_id="e", field="ExDestination(100)")
    assert f.exception_type is ExceptionType.UNKNOWN_DESTINATION
    f = resolution_finding(r("NOPE", registry, mic_reference), source_event_id="e", field="LastMkt(30)", execution=True)
    assert f.exception_type is ExceptionType.UNRESOLVED_EXECUTION_VENUE
    f = resolution_finding(r("SMART", registry, mic_reference, source_system="BROKER_B"), source_event_id="e", field="x")
    assert f.exception_type is ExceptionType.AMBIGUOUS_VENUE
