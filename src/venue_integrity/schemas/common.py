"""Shared Pydantic building blocks."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, PlainSerializer

from venue_integrity.timeutil import ensure_utc, isoformat

UtcDatetime = Annotated[
    datetime,
    AfterValidator(ensure_utc),
    PlainSerializer(lambda v: isoformat(v), return_type=str, when_used="json"),
]

# Namespace for deterministic event identifiers (replay must reproduce IDs).
VIA_NAMESPACE = uuid.UUID("6f0d7f3e-6a55-4f37-9c39-2a1f2b8f5a10")


def sha256_hex(payload: str | bytes) -> str:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(data).hexdigest()


def deterministic_event_id(
    source_system: str, session_id: str | None, sequence_number: int | None, payload_hash: str
) -> str:
    """Event ID derived from immutable message identity, independent of arrival time.

    Re-delivery of identical bytes on the same session/sequence yields the same ID
    (idempotent ingestion); a retransmission with PossDupFlag carries different
    header bytes and is therefore a distinct, separately retained raw event.
    """
    key = f"{source_system}|{session_id or ''}|{'' if sequence_number is None else sequence_number}|{payload_hash}"
    return str(uuid.uuid5(VIA_NAMESPACE, key))


class StrictModel(BaseModel):
    """Immutable, closed-world model: unknown fields are rejected, not dropped."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)
