"""Message-level validation: protocol, dictionary, enumerations and timestamps.

Validation never mutates or drops a message. It returns a status and a list of
findings; a message that fails validation remains in the raw store for
investigation (spec §3.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from venue_integrity.identity.exception_queue import Finding
from venue_integrity.ingestion.fix_parser import FixDictionary, FixMessage
from venue_integrity.schemas.enums import ExceptionType
from venue_integrity.schemas.raw_event import RawEvent
from venue_integrity.timeutil import TimestampError, parse_fix_timestamp, to_epoch_ns

ENUM_EXCEPTION = {
    40: ExceptionType.UNKNOWN_ORDER_TYPE_CODE,
    54: ExceptionType.INVALID_SIDE,
}
TIMESTAMP_TAGS = (52, 60, 122)


class ValidationStatus(StrEnum):
    VALID = "VALID"
    VALID_WITH_WARNINGS = "VALID_WITH_WARNINGS"
    INVALID = "INVALID"


@dataclass
class ValidationResult:
    status: ValidationStatus
    findings: list[Finding] = field(default_factory=list)
    timestamps: dict[int, tuple[datetime, int]] = field(default_factory=dict)

    @property
    def is_usable(self) -> bool:
        """Structurally sound enough to normalise (findings still apply)."""
        return self.status is not ValidationStatus.INVALID


def validate_message(
    message: FixMessage,
    raw: RawEvent,
    dictionary: FixDictionary,
    *,
    clock_drift_tolerance_ms: int = 2000,
    future_timestamp_tolerance_ms: int = 500,
) -> ValidationResult:
    findings: list[Finding] = []
    invalid = False
    eid = raw.event_id

    if message.issues:
        invalid = True
        findings.append(
            Finding(
                ExceptionType.PROTOCOL_VALIDATION_FAILURE,
                "; ".join(f"{i.code}: {i.detail}" for i in message.issues),
                source_event_id=eid,
                context={"issues": [i.code for i in message.issues]},
            )
        )

    protocol = message.begin_string
    if not dictionary.supports_protocol(protocol):
        invalid = True
        findings.append(
            Finding(
                ExceptionType.PROTOCOL_VALIDATION_FAILURE,
                f"unsupported protocol {protocol!r} for dictionary {dictionary.version}",
                source_event_id=eid,
                raw_value=protocol,
            )
        )

    msg_type = message.msg_type
    if msg_type is None:
        invalid = True
        findings.append(Finding(ExceptionType.PROTOCOL_VALIDATION_FAILURE, "missing MsgType(35)", source_event_id=eid))

    # Unknown tags: retained, flagged for dictionary review.
    for tag in sorted(set(message.tags())):
        if not dictionary.is_known(tag, raw.source_system):
            findings.append(
                Finding(
                    ExceptionType.UNKNOWN_FIX_TAG,
                    f"tag {tag} not in dictionary {dictionary.version} for {raw.source_system}",
                    source_event_id=eid,
                    entity_key=str(tag),
                    raw_value=message.get(tag),
                )
            )

    if protocol and msg_type and dictionary.supports_protocol(protocol):
        for tag in dictionary.required_tags(protocol, msg_type):
            if not message.has(tag) or message.get(tag) == "":
                exc = ExceptionType.MISSING_ORDER_TYPE if tag == 40 else ExceptionType.PROTOCOL_VALIDATION_FAILURE
                findings.append(
                    Finding(
                        exc,
                        f"required tag {tag} ({dictionary.tag_name(tag)}) missing on MsgType {msg_type}",
                        source_event_id=eid,
                        entity_key=str(tag),
                    )
                )

    # Enumerations: report, never coerce.
    for tag, value in message.fields:
        allowed = dictionary.enum_values(tag)
        if allowed is not None and value not in allowed:
            findings.append(
                Finding(
                    ENUM_EXCEPTION.get(tag, ExceptionType.UNKNOWN_ENUM_VALUE),
                    f"tag {tag} value {value!r} not in dictionary enumeration",
                    source_event_id=eid,
                    entity_key=str(tag),
                    raw_value=value,
                )
            )

    timestamps: dict[int, tuple[datetime, int]] = {}
    for tag in TIMESTAMP_TAGS:
        value = message.get(tag)
        if value is None:
            continue
        try:
            timestamps[tag] = parse_fix_timestamp(value)
        except TimestampError as exc:
            findings.append(
                Finding(
                    ExceptionType.INVALID_TIMESTAMP,
                    str(exc),
                    source_event_id=eid,
                    entity_key=str(tag),
                    raw_value=value,
                )
            )

    if 52 in timestamps:
        _, sending_ns = timestamps[52]
        received_ns = to_epoch_ns(raw.received_at)
        lag_ms = (received_ns - sending_ns) / 1e6
        if lag_ms > clock_drift_tolerance_ms or -lag_ms > future_timestamp_tolerance_ms:
            findings.append(
                Finding(
                    ExceptionType.CLOCK_DRIFT,
                    f"receipt minus SendingTime = {lag_ms:.3f} ms outside tolerance "
                    f"[-{future_timestamp_tolerance_ms}, {clock_drift_tolerance_ms}] ms",
                    source_event_id=eid,
                    entity_key="52",
                    context={"lag_ms": round(lag_ms, 3)},
                )
            )

    if invalid:
        status = ValidationStatus.INVALID
    elif findings:
        status = ValidationStatus.VALID_WITH_WARNINGS
    else:
        status = ValidationStatus.VALID
    return ValidationResult(status=status, findings=findings, timestamps=timestamps)
