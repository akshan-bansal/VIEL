"""Venue identity resolution (spec §3.2, §5.2, §11.3).

Rules, in order:

1. Missing/empty identifier -> UNMAPPED (``MISSING_IDENTIFIER``).
2. Exact match only: no trimming, case folding or fuzzy matching. An alias
   matches a record's ``raw_aliases`` or that broker's ``broker_aliases``; a MIC
   matches a record's ``mic``. Which kind the raw value is comes from the
   protocol (e.g. FIX 1133=G), never from what the string looks like.
3. Only records in force in the requested registry version (system time) and
   effective at the event time (business time) are considered.
4. No effective match -> UNMAPPED. Matches pointing at more than one venue ->
   AMBIGUOUS (no venue chosen). One venue whose record is not ACTIVE or whose
   MIC fails the authoritative reference -> INVALID (no venue returned).
5. Otherwise MAPPED.

``venue_id`` is populated only for MAPPED. There is no default venue and no
fallback category.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from venue_integrity.identity.exception_queue import Finding
from venue_integrity.identity.mic_registry import MicReference, VenueRegistry
from venue_integrity.schemas.enums import ExceptionType, IdentifierType, MappingStatus, VenueStatus
from venue_integrity.schemas.venue_identity import ResolutionCandidate, VenueRecord, VenueResolution


def _candidate(record: VenueRecord, match_type: str) -> ResolutionCandidate:
    return ResolutionCandidate(
        venue_id=record.venue_id,
        mic=record.mic,
        match_type=match_type,
        mapping_version=record.mapping_version,
        valid_from=record.valid_from,
        valid_to=record.valid_to,
        status=record.status,
    )


def _effective(record: VenueRecord, event_time: datetime | None) -> bool:
    if event_time is None:
        # Without an event time only time-independent records can apply.
        return record.valid_to is None and record.valid_from.year == 1
    return record.is_effective(event_time)


def resolve_venue(
    raw_value: str | None,
    registry: VenueRegistry | Mapping[str, list[dict[str, Any]]],
    mapping_version: str | None = None,
    *,
    event_time: datetime | None = None,
    source_system: str | None = None,
    identifier_type: IdentifierType = IdentifierType.ALIAS,
    mic_reference: MicReference | None = None,
) -> VenueResolution:
    if not isinstance(registry, VenueRegistry):
        registry = VenueRegistry.from_alias_map(dict(registry), version=mapping_version or "UNVERSIONED")
        mapping_version = registry.latest_version
    version = mapping_version if mapping_version is not None else registry.latest_version
    if not registry.has_version(version):
        return VenueResolution(
            raw_value=raw_value,
            identifier_type=identifier_type,
            source_system=source_system,
            event_time=event_time,
            mapping_status=MappingStatus.INVALID,
            mapping_version=version,
            reason="UNKNOWN_REGISTRY_VERSION",
        )

    base = dict(
        raw_value=raw_value,
        identifier_type=identifier_type,
        source_system=source_system,
        event_time=event_time,
        mapping_version=version,
    )
    if raw_value is None or raw_value == "":
        return VenueResolution(**base, mapping_status=MappingStatus.UNMAPPED, reason="MISSING_IDENTIFIER")

    matched: list[tuple[VenueRecord, str]] = []
    for record in registry.records_at(version):
        match_type = record.matches(raw_value, identifier_type, source_system)
        if match_type is not None:
            matched.append((record, match_type))

    if not matched:
        return VenueResolution(**base, mapping_status=MappingStatus.UNMAPPED, reason="NO_MATCHING_IDENTIFIER")

    effective = [(r, m) for r, m in matched if _effective(r, event_time)]
    if not effective:
        return VenueResolution(
            **base,
            mapping_status=MappingStatus.UNMAPPED,
            reason="IDENTIFIER_NOT_EFFECTIVE_AT_EVENT_TIME",
            candidates=tuple(_candidate(r, m) for r, m in matched),
        )

    candidates = tuple(
        sorted((_candidate(r, m) for r, m in effective), key=lambda c: (c.venue_id, c.match_type, c.valid_from))
    )
    venue_ids = sorted({c.venue_id for c in candidates})
    if len(venue_ids) > 1:
        return VenueResolution(
            **base,
            mapping_status=MappingStatus.AMBIGUOUS,
            reason=f"IDENTIFIER_MATCHES_{len(venue_ids)}_VENUES",
            candidates=candidates,
        )

    records = [r for r, _ in effective]
    mics = {r.mic for r in records}
    if len(mics) > 1:
        return VenueResolution(
            **base,
            mapping_status=MappingStatus.AMBIGUOUS,
            reason="CONFLICTING_MIC_FOR_SAME_VENUE",
            candidates=candidates,
        )
    record = records[0]
    if any(r.status is not VenueStatus.ACTIVE for r in records):
        return VenueResolution(
            **base, mapping_status=MappingStatus.INVALID, reason=f"VENUE_STATUS_{record.status}", candidates=candidates
        )
    if mic_reference is not None:
        ok, mic_reason = mic_reference.validate(record.mic)
        if not ok:
            return VenueResolution(**base, mapping_status=MappingStatus.INVALID, reason=mic_reason, candidates=candidates)

    return VenueResolution(
        **base,
        venue_id=record.venue_id,
        mic=record.mic,
        mapping_status=MappingStatus.MAPPED,
        match_type=candidates[0].match_type,
        reason="MAPPED",
        candidates=candidates,
    )


EXCEPTION_FOR_STATUS = {
    MappingStatus.UNMAPPED: ExceptionType.UNKNOWN_DESTINATION,
    MappingStatus.AMBIGUOUS: ExceptionType.AMBIGUOUS_VENUE,
    MappingStatus.INVALID: ExceptionType.INVALID_VENUE,
}


def resolution_finding(
    resolution: VenueResolution, *, source_event_id: str | None, field: str, execution: bool = False
) -> Finding | None:
    """Exception for a non-MAPPED resolution (None when MAPPED)."""
    if resolution.mapping_status is MappingStatus.MAPPED:
        return None
    exc = EXCEPTION_FOR_STATUS[resolution.mapping_status]
    if execution and resolution.mapping_status is MappingStatus.UNMAPPED:
        exc = ExceptionType.UNRESOLVED_EXECUTION_VENUE
    return Finding(
        exc,
        f"{field}={resolution.raw_value!r} -> {resolution.mapping_status} ({resolution.reason}) "
        f"under registry {resolution.mapping_version}",
        source_event_id=source_event_id,
        entity_key=field,
        raw_value=resolution.raw_value,
        context={
            "source_system": resolution.source_system,
            "registry_version": resolution.mapping_version,
            "candidates": [c.venue_id for c in resolution.candidates],
            "reason": resolution.reason,
        },
    )
