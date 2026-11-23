"""Maker-checker change control for venue reference data (spec §3.2, §14).

A change is proposed by one principal and approved by a different one.
Approval creates a new registry version; records are superseded, never edited,
so every earlier version remains replayable. Each approval also produces a
MAPPING_VERSION_CHANGE finding (alert VENUE-007: revalidate and retain audit
trail).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from venue_integrity.identity.exception_queue import Finding
from venue_integrity.identity.mic_registry import MicReference, RegistryVersion, VenueRegistry, record_key
from venue_integrity.schemas.common import VIA_NAMESPACE
from venue_integrity.schemas.enums import ExceptionType
from venue_integrity.schemas.venue_identity import VenueRecord
from venue_integrity.timeutil import isoformat, parse_iso


class ChangeStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class MakerCheckerError(PermissionError):
    pass


@dataclass
class ChangeRequest:
    request_id: str
    proposed_by: str
    proposed_at: datetime
    description: str
    evidence: str
    add_records: list[dict[str, Any]]
    supersede_keys: list[str]
    status: ChangeStatus = ChangeStatus.PROPOSED
    approved_by: str | None = None
    decided_at: datetime | None = None
    resulting_version: str | None = None
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "proposed_by": self.proposed_by,
            "proposed_at": isoformat(self.proposed_at),
            "description": self.description,
            "evidence": self.evidence,
            "add_records": self.add_records,
            "supersede_keys": self.supersede_keys,
            "status": self.status.value,
            "approved_by": self.approved_by,
            "decided_at": isoformat(self.decided_at),
            "resulting_version": self.resulting_version,
            "rejection_reason": self.rejection_reason,
        }


def _record_from_dict(item: dict[str, Any], version: str, recorded_at: datetime, approved_by: str | None,
                      request_id: str | None) -> VenueRecord:
    def ts(value):
        if value is None or isinstance(value, datetime):
            return value
        return parse_iso(str(value))

    return VenueRecord(
        venue_id=item["venue_id"],
        mic=item.get("mic"),
        operating_mic=item.get("operating_mic"),
        raw_aliases=tuple(item.get("raw_aliases") or ()),
        broker_aliases={k: tuple(v) for k, v in (item.get("broker_aliases") or {}).items()},
        venue_name=item.get("venue_name"),
        venue_type=item.get("venue_type", "OTHER"),
        country=item.get("country"),
        valid_from=ts(item["valid_from"]),
        valid_to=ts(item.get("valid_to")),
        mapping_version=version,
        status=item.get("status", "ACTIVE"),
        approved_by=approved_by,
        recorded_at=recorded_at,
        change_request_id=request_id,
    )


class MappingChangeManager:
    def __init__(self, registry: VenueRegistry, mic_reference: MicReference | None = None):
        self.registry = registry
        self.mic_reference = mic_reference
        self._requests: dict[str, ChangeRequest] = {}
        self.audit_log: list[dict[str, Any]] = []

    def propose(
        self,
        *,
        proposed_by: str,
        description: str,
        evidence: str,
        add_records: list[dict[str, Any]] | None = None,
        supersede: list[VenueRecord] | None = None,
        at: datetime,
    ) -> ChangeRequest:
        if not evidence.strip():
            raise ValueError("a reference-data change requires supporting evidence")
        add_records = [dict(r) for r in (add_records or [])]
        supersede_keys = [record_key(r) for r in (supersede or [])]
        if not add_records and not supersede_keys:
            raise ValueError("empty change request")
        current = {record_key(r) for r in self.registry.records_at(None)}
        for key in supersede_keys:
            if key not in current:
                raise ValueError("cannot supersede a record that is not in the current registry version")
        # Validate the proposed records up-front (format, interval, MIC reference).
        for item in add_records:
            record = _record_from_dict(item, self.registry.latest_version, at, None, None)
            if self.mic_reference is not None and record.mic is not None:
                ok, reason = self.mic_reference.validate(record.mic)
                if not ok:
                    raise ValueError(f"proposed MIC {record.mic} rejected by authoritative reference: {reason}")
        request_id = str(uuid.uuid5(VIA_NAMESPACE, f"change|{proposed_by}|{isoformat(at)}|{description}"))
        request = ChangeRequest(
            request_id=request_id,
            proposed_by=proposed_by,
            proposed_at=at,
            description=description,
            evidence=evidence,
            add_records=add_records,
            supersede_keys=supersede_keys,
        )
        self._requests[request_id] = request
        self.audit_log.append({"at": isoformat(at), "action": "PROPOSED", "by": proposed_by, "request_id": request_id})
        return request

    def approve(self, request_id: str, *, approved_by: str, new_version: str, at: datetime) -> tuple[VenueRegistry, Finding]:
        request = self._requests[request_id]
        if request.status is not ChangeStatus.PROPOSED:
            raise ValueError(f"request {request_id} is {request.status}")
        if approved_by == request.proposed_by:
            raise MakerCheckerError("maker-checker violation: proposer cannot approve their own change")
        previous = self.registry.latest_version
        added = [_record_from_dict(item, new_version, at, approved_by, request_id) for item in request.add_records]
        keys = set(request.supersede_keys)
        superseded = [r for r in self.registry.records_at(None) if record_key(r) in keys]
        version = RegistryVersion(new_version, at, approved_by, request_id, request.description)
        self.registry = self.registry.with_new_version(version, added, superseded)
        request.status = ChangeStatus.APPROVED
        request.approved_by = approved_by
        request.decided_at = at
        request.resulting_version = new_version
        self.audit_log.append(
            {"at": isoformat(at), "action": "APPROVED", "by": approved_by, "request_id": request_id,
             "from_version": previous, "to_version": new_version}
        )
        finding = Finding(
            ExceptionType.MAPPING_VERSION_CHANGE,
            f"registry {previous} -> {new_version}: {request.description}",
            entity_key=f"registry|{new_version}",
            context={"request_id": request_id, "proposed_by": request.proposed_by, "approved_by": approved_by,
                     "from_version": previous, "to_version": new_version},
        )
        return self.registry, finding

    def reject(self, request_id: str, *, rejected_by: str, reason: str, at: datetime) -> ChangeRequest:
        request = self._requests[request_id]
        if request.status is not ChangeStatus.PROPOSED:
            raise ValueError(f"request {request_id} is {request.status}")
        request.status = ChangeStatus.REJECTED
        request.approved_by = rejected_by
        request.decided_at = at
        request.rejection_reason = reason
        self.audit_log.append({"at": isoformat(at), "action": "REJECTED", "by": rejected_by, "request_id": request_id})
        return request

    def requests(self) -> list[ChangeRequest]:
        return sorted(self._requests.values(), key=lambda r: (r.proposed_at, r.request_id))

    def get(self, request_id: str) -> ChangeRequest:
        return self._requests[request_id]


@dataclass
class MappingHistory:
    """Convenience view over registry versions for reports."""

    registry: VenueRegistry
    changes: list[ChangeRequest] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "versions": [
                {"version": v.version, "created_at": isoformat(v.created_at), "approved_by": v.approved_by,
                 "change_request_id": v.change_request_id, "description": v.description}
                for v in self.registry.versions
            ],
            "changes": [c.to_dict() for c in self.changes],
        }
