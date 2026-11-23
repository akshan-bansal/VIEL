"""Contract A — raw market event (spec §3.1).

The raw event is created before any normalisation and never modified. The
payload hash is verified on construction, so an object of this type cannot
exist with a payload that does not match its recorded hash.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from venue_integrity.schemas.common import StrictModel, UtcDatetime, sha256_hex


class RawEvent(StrictModel):
    event_id: str
    source_system: str = Field(min_length=1)
    session_id: str | None = None
    sequence_number: int | None = Field(default=None, ge=0)
    received_at: UtcDatetime
    source_timestamp: UtcDatetime | None = None
    # Exact source timestamp in epoch nanoseconds (FIX allows up to 9 fractional digits).
    source_timestamp_ns: int | None = None
    protocol: str | None = None
    message_type: str | None = None
    raw_payload: str = Field(min_length=1)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: str = "1.0"

    @model_validator(mode="after")
    def _verify_hash(self) -> RawEvent:
        actual = sha256_hex(self.raw_payload)
        if actual != self.payload_hash:
            raise ValueError(
                f"payload_hash mismatch for event {self.event_id}: recorded {self.payload_hash}, actual {actual}"
            )
        return self


class RawCapture(StrictModel):
    """Envelope for a captured message before it is assigned an event ID."""

    source_system: str = Field(min_length=1)
    session_id: str | None = None
    received_at: UtcDatetime
    raw_payload: str = Field(min_length=1)
    protocol_hint: str | None = None
