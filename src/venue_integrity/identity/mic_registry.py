"""Authoritative MIC reference and the versioned venue registry (spec §3.2).

``MicReference`` loads an ISO 10383 snapshot (the standard CSV layout). A MIC is
valid only if it appears in the snapshot with ACTIVE status — never because a
string merely looks like a MIC.

``VenueRegistry`` is bitemporal:

* business time — each record's ``valid_from``/``valid_to``;
* system time — an ordered list of registry versions. A record belongs to the
  version that introduced it until a later version supersedes it.

Resolving with (registry version, event time) is therefore deterministic and
any historical version can be replayed exactly.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from venue_integrity.schemas.enums import VenueStatus
from venue_integrity.schemas.venue_identity import MIC_PATTERN, VenueRecord
from venue_integrity.timeutil import MIN_TIME, isoformat, parse_iso


@dataclass(frozen=True)
class MicEntry:
    mic: str
    operating_mic: str
    kind: str
    name: str
    country: str
    status: str


class MicReference:
    def __init__(self, entries: dict[str, MicEntry], source: str, snapshot_hash: str):
        self.entries = entries
        self.source = source
        self.snapshot_hash = snapshot_hash

    @classmethod
    def from_csv(cls, path: str | Path) -> MicReference:
        path = Path(path)
        data = path.read_bytes()
        entries: dict[str, MicEntry] = {}
        reader = csv.DictReader(data.decode("utf-8").splitlines())
        for row in reader:
            mic = row["MIC"].strip()
            entries[mic] = MicEntry(
                mic=mic,
                operating_mic=row.get("OPERATING MIC", "").strip(),
                kind=row.get("OPRT/SGMT", "").strip(),
                name=row.get("MARKET NAME-INSTITUTION DESCRIPTION", "").strip(),
                country=row.get("ISO COUNTRY CODE (ISO 3166)", "").strip(),
                status=row.get("STATUS", "").strip().upper(),
            )
        return cls(entries, str(path), hashlib.sha256(data).hexdigest())

    def validate(self, mic: str | None) -> tuple[bool, str]:
        if mic is None:
            return False, "MIC_MISSING"
        if not MIC_PATTERN.match(mic):
            return False, "MIC_FORMAT_INVALID"
        entry = self.entries.get(mic)
        if entry is None:
            return False, "MIC_NOT_IN_REFERENCE"
        if entry.status != "ACTIVE":
            return False, f"MIC_STATUS_{entry.status}"
        return True, "MIC_VALID"


def record_key(record: VenueRecord) -> str:
    """Content identity of a record, independent of when it was superseded."""
    body = record.model_dump(mode="json", exclude={"superseded_in_version"})
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class RegistryVersion:
    version: str
    created_at: datetime
    approved_by: str | None
    change_request_id: str | None
    description: str


class RegistryVersionError(KeyError):
    pass


class VenueRegistry:
    """Append-only collection of venue records across registry versions."""

    def __init__(self, records: list[VenueRecord], versions: list[RegistryVersion]):
        if not versions:
            raise ValueError("registry requires at least one version")
        names = [v.version for v in versions]
        if len(set(names)) != len(names):
            raise ValueError("duplicate registry versions")
        self._versions = list(versions)
        self._order = {v.version: i for i, v in enumerate(self._versions)}
        for record in records:
            self._check_record_versions(record)
        self._records = list(records)

    def _check_record_versions(self, record: VenueRecord) -> None:
        if record.mapping_version not in self._order:
            raise RegistryVersionError(f"record introduced in unknown version {record.mapping_version}")
        if record.superseded_in_version is not None:
            if record.superseded_in_version not in self._order:
                raise RegistryVersionError(f"record superseded in unknown version {record.superseded_in_version}")
            if self._order[record.superseded_in_version] <= self._order[record.mapping_version]:
                raise ValueError("a record must be superseded by a later version")

    # -- construction -------------------------------------------------------
    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> VenueRegistry:
        version = str(raw["registry_version"])
        created_at = parse_iso(str(raw["registry_created_at"]))
        approved_by = raw.get("approved_by")
        records = []
        for item in raw.get("records", []):
            records.append(
                VenueRecord(
                    venue_id=item["venue_id"],
                    mic=item.get("mic"),
                    operating_mic=item.get("operating_mic"),
                    raw_aliases=tuple(item.get("raw_aliases") or ()),
                    broker_aliases={k: tuple(v) for k, v in (item.get("broker_aliases") or {}).items()},
                    venue_name=item.get("venue_name"),
                    venue_type=item.get("venue_type", "OTHER"),
                    country=item.get("country"),
                    valid_from=parse_iso(str(item["valid_from"])),
                    valid_to=parse_iso(str(item["valid_to"])) if item.get("valid_to") else None,
                    mapping_version=version,
                    status=item.get("status", "ACTIVE"),
                    approved_by=item.get("approved_by", approved_by),
                    recorded_at=created_at,
                )
            )
        versions = [RegistryVersion(version, created_at, approved_by, None, "initial load from configuration")]
        return cls(records, versions)

    @classmethod
    def from_alias_map(cls, alias_map: dict[str, list[dict[str, Any]]], version: str = "UNVERSIONED") -> VenueRegistry:
        """Build a time-independent registry from ``{alias: [{venue_id, mic}, ...]}``.

        Supports the simple mapping form used in the specification's examples.
        """
        records = []
        for alias in sorted(alias_map):
            for entry in alias_map[alias]:
                records.append(
                    VenueRecord(
                        venue_id=entry["venue_id"],
                        mic=entry.get("mic"),
                        raw_aliases=(alias,),
                        valid_from=MIN_TIME,
                        mapping_version=version,
                        status=entry.get("status", VenueStatus.ACTIVE),
                    )
                )
        return cls(records, [RegistryVersion(version, MIN_TIME, None, None, "alias map")])

    # -- versions -----------------------------------------------------------
    @property
    def versions(self) -> list[RegistryVersion]:
        return list(self._versions)

    @property
    def latest_version(self) -> str:
        return self._versions[-1].version

    def has_version(self, version: str) -> bool:
        return version in self._order

    def _version_index(self, version: str | None) -> int:
        if version is None:
            return len(self._versions) - 1
        try:
            return self._order[version]
        except KeyError as exc:
            raise RegistryVersionError(f"unknown registry version {version!r}") from exc

    def records_at(self, version: str | None = None) -> list[VenueRecord]:
        """Records in force in the given registry version (system time)."""
        idx = self._version_index(version)
        out = []
        for record in self._records:
            introduced = self._order[record.mapping_version]
            superseded = (
                self._order[record.superseded_in_version] if record.superseded_in_version is not None else None
            )
            if introduced <= idx and (superseded is None or superseded > idx):
                out.append(record)
        return out

    def all_records(self) -> list[VenueRecord]:
        return list(self._records)

    def venue_ids(self, version: str | None = None) -> list[str]:
        return sorted({r.venue_id for r in self.records_at(version)})

    def venue_metadata(self, venue_id: str, version: str | None = None, event_time: datetime | None = None):
        records = [r for r in self.records_at(version) if r.venue_id == venue_id]
        if event_time is not None:
            records = [r for r in records if r.is_effective(event_time)]
        return sorted(records, key=lambda r: (r.valid_from, r.mapping_version))

    def with_new_version(
        self,
        version: RegistryVersion,
        added: list[VenueRecord],
        superseded: list[VenueRecord],
    ) -> VenueRegistry:
        """Return a new registry with one more version. The receiver is unchanged."""
        if version.version in self._order:
            raise ValueError(f"version {version.version} already exists")
        new_versions = [*self._versions, version]
        superseded_keys = {record_key(r) for r in superseded}
        current_keys = {record_key(r) for r in self.records_at(None)}
        missing = superseded_keys - current_keys
        if missing:
            raise ValueError("cannot supersede a record that is not current")
        records: list[VenueRecord] = []
        for record in self._records:
            if record_key(record) in superseded_keys and record.superseded_in_version is None:
                record = record.model_copy(update={"superseded_in_version": version.version})
            records.append(record)
        for record in added:
            if record.mapping_version != version.version:
                raise ValueError("added records must carry the new mapping_version")
            records.append(record)
        return VenueRegistry(records, new_versions)

    # -- evidence -----------------------------------------------------------
    def snapshot(self, version: str | None = None) -> dict[str, Any]:
        version_name = self._versions[self._version_index(version)].version
        records = sorted(
            (r.model_dump(mode="json") for r in self.records_at(version)),
            key=lambda d: (d["venue_id"], d["valid_from"], json.dumps(d, sort_keys=True)),
        )
        body = json.dumps(records, sort_keys=True, separators=(",", ":"))
        return {
            "registry_version": version_name,
            "record_count": len(records),
            "snapshot_hash": hashlib.sha256(body.encode()).hexdigest(),
            "records": records,
            "version_history": [
                {
                    "version": v.version,
                    "created_at": isoformat(v.created_at),
                    "approved_by": v.approved_by,
                    "change_request_id": v.change_request_id,
                    "description": v.description,
                }
                for v in self._versions[: self._version_index(version) + 1]
            ],
        }
