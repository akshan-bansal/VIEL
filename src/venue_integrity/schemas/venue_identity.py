"""Contract B — venue identity (spec §3.2) and resolution results."""

from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator

from venue_integrity.schemas.common import StrictModel, UtcDatetime
from venue_integrity.schemas.enums import (
    IdentifierType,
    MappingStatus,
    VenueStatus,
    VenueType,
)

MIC_PATTERN = re.compile(r"^[A-Z0-9]{4}$")


class VenueRecord(StrictModel):
    """One immutable, effective-dated reference-data record.

    ``valid_from``/``valid_to`` is business time (when the aliases meant this
    venue). ``mapping_version`` is the registry version that introduced the
    record and ``superseded_in_version`` the version that withdrew it (system
    time). Records are never edited; they are superseded.
    """

    venue_id: str = Field(min_length=1)
    mic: str | None = None
    operating_mic: str | None = None
    raw_aliases: tuple[str, ...] = ()
    broker_aliases: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    venue_name: str | None = None
    venue_type: VenueType = VenueType.OTHER
    country: str | None = Field(default=None, min_length=2, max_length=2)
    valid_from: UtcDatetime
    valid_to: UtcDatetime | None = None
    mapping_version: str
    superseded_in_version: str | None = None
    status: VenueStatus = VenueStatus.ACTIVE
    approved_by: str | None = None
    recorded_at: UtcDatetime | None = None
    change_request_id: str | None = None

    @field_validator("mic", "operating_mic")
    @classmethod
    def _mic_format(cls, value: str | None) -> str | None:
        if value is not None and not MIC_PATTERN.match(value):
            raise ValueError(f"MIC {value!r} is not a 4-character ISO 10383 code")
        return value

    @model_validator(mode="after")
    def _interval(self) -> VenueRecord:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self

    def is_effective(self, event_time) -> bool:
        if event_time is None:
            return False
        return self.valid_from <= event_time and (self.valid_to is None or event_time < self.valid_to)

    def matches(self, raw_value: str, identifier_type: IdentifierType, source_system: str | None) -> str | None:
        """Return the match type if ``raw_value`` identifies this record, else None.

        Matching is exact (byte-for-byte). No case folding, trimming or fuzzy
        matching: such transformations would be an implicit mapping decision.
        """
        if identifier_type is IdentifierType.MIC:
            return "MIC" if self.mic is not None and raw_value == self.mic else None
        if raw_value in self.raw_aliases:
            return "ALIAS"
        if source_system is not None and raw_value in self.broker_aliases.get(source_system, ()):
            return "BROKER_ALIAS"
        return None


class ResolutionCandidate(StrictModel):
    venue_id: str
    mic: str | None
    match_type: str
    mapping_version: str
    valid_from: UtcDatetime
    valid_to: UtcDatetime | None
    status: VenueStatus


class VenueResolution(StrictModel):
    """Outcome of resolving one raw venue identifier.

    ``venue_id`` is populated only when ``mapping_status`` is MAPPED. For every
    other status the raw value and the candidates are retained for investigation.
    """

    raw_value: str | None
    identifier_type: IdentifierType
    source_system: str | None = None
    event_time: UtcDatetime | None = None
    venue_id: str | None = None
    mic: str | None = None
    mapping_status: MappingStatus
    mapping_version: str
    match_type: str | None = None
    reason: str
    candidates: tuple[ResolutionCandidate, ...] = ()

    @model_validator(mode="after")
    def _status_consistency(self) -> VenueResolution:
        if self.mapping_status is MappingStatus.MAPPED and self.venue_id is None:
            raise ValueError("MAPPED resolution must carry a venue_id")
        if self.mapping_status is not MappingStatus.MAPPED and self.venue_id is not None:
            raise ValueError("only a MAPPED resolution may carry a venue_id")
        return self
