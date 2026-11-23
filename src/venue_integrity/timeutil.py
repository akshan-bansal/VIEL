"""UTC time helpers.

All timestamps inside the audit are timezone-aware UTC ``datetime`` objects.
Where a source carries sub-microsecond precision, the integer epoch-nanosecond
value is carried alongside (``*_ns`` fields) so that ordering and latency
calculations do not lose precision; the raw payload always retains the
original string.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_TIME = datetime(1, 1, 1, tzinfo=UTC)

_FIX_TS = re.compile(
    r"^(?P<date>\d{8})-(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})(?:\.(?P<frac>\d{1,9}))?$"
)


class TimestampError(ValueError):
    """Raised when a timestamp cannot be parsed without guessing."""


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TimestampError(f"naive datetime not allowed: {value!r}")
    return value.astimezone(UTC)


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp. A missing offset is an error, not 'assume UTC'."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimestampError(f"invalid ISO timestamp {value!r}") from exc
    return ensure_utc(parsed)


def to_epoch_ns(value: datetime) -> int:
    value = ensure_utc(value)
    delta = value - EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def from_epoch_ns(ns: int) -> datetime:
    return EPOCH + timedelta(microseconds=ns // 1_000)


def parse_fix_timestamp(value: str) -> tuple[datetime, int]:
    """Parse a FIX UTCTimestamp ``YYYYMMDD-HH:MM:SS[.fff[fff[fff]]]``.

    Returns ``(datetime truncated to microseconds, exact epoch nanoseconds)``.
    """
    match = _FIX_TS.match(value)
    if not match:
        raise TimestampError(f"invalid FIX UTCTimestamp {value!r}")
    date = match.group("date")
    try:
        base = datetime(
            int(date[0:4]),
            int(date[4:6]),
            int(date[6:8]),
            int(match.group("h")),
            int(match.group("m")),
            int(match.group("s")),
            tzinfo=UTC,
        )
    except ValueError as exc:
        raise TimestampError(f"invalid FIX UTCTimestamp {value!r}") from exc
    frac = match.group("frac") or ""
    nanos = int(frac.ljust(9, "0")) if frac else 0
    ns = to_epoch_ns(base) + nanos
    return base + timedelta(microseconds=nanos // 1_000), ns


def format_fix_timestamp(value: datetime, digits: int = 6) -> str:
    value = ensure_utc(value)
    base = value.strftime("%Y%m%d-%H:%M:%S")
    if digits <= 0:
        return base
    frac = f"{value.microsecond:06d}".ljust(9, "0")[:digits]
    return f"{base}.{frac}"


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_utc(value).isoformat().replace("+00:00", "Z")
