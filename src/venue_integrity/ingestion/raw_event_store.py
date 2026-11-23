"""Append-only, tamper-evident raw event store (spec §3.1, §14).

Prototype backend: SQLite. ``UPDATE`` and ``DELETE`` on ``raw_events`` are
rejected by database triggers, every payload hash is re-verified on read, and
each row is linked into a SHA-256 hash chain so that any alteration, deletion
or reordering of stored history is detectable.

Ingestion is idempotent: re-delivery of the identical message (same session,
sequence number and bytes) is logged as a duplicate delivery and does not
create a second raw event. The production schema (database/schema.sql)
implements the same controls in PostgreSQL.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from venue_integrity.ingestion.fix_parser import parse_fix
from venue_integrity.schemas.common import deterministic_event_id, sha256_hex
from venue_integrity.schemas.raw_event import RawCapture, RawEvent
from venue_integrity.timeutil import TimestampError, isoformat, parse_fix_timestamp, parse_iso

GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_events (
    chain_index       INTEGER PRIMARY KEY,
    event_id          TEXT NOT NULL UNIQUE,
    source_system     TEXT NOT NULL,
    session_id        TEXT,
    sequence_number   INTEGER,
    received_at       TEXT NOT NULL,
    source_timestamp  TEXT,
    source_timestamp_ns INTEGER,
    protocol          TEXT,
    message_type      TEXT,
    raw_payload       TEXT NOT NULL,
    payload_hash      TEXT NOT NULL,
    schema_version    TEXT NOT NULL,
    prev_chain_hash   TEXT NOT NULL,
    chain_hash        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_log (
    delivery_index INTEGER PRIMARY KEY,
    event_id       TEXT NOT NULL,
    received_at    TEXT NOT NULL,
    outcome        TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS raw_events_no_update BEFORE UPDATE ON raw_events
BEGIN SELECT RAISE(ABORT, 'raw_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS raw_events_no_delete BEFORE DELETE ON raw_events
BEGIN SELECT RAISE(ABORT, 'raw_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS delivery_log_no_update BEFORE UPDATE ON delivery_log
BEGIN SELECT RAISE(ABORT, 'delivery_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS delivery_log_no_delete BEFORE DELETE ON delivery_log
BEGIN SELECT RAISE(ABORT, 'delivery_log is append-only'); END;
"""


class AppendOutcome(StrEnum):
    STORED = "STORED"
    DUPLICATE_DELIVERY = "DUPLICATE_DELIVERY"


@dataclass(frozen=True)
class AppendResult:
    outcome: AppendOutcome
    event: RawEvent


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    count: int
    head_hash: str
    broken_at: int | None = None
    reason: str | None = None


class IntegrityError(RuntimeError):
    pass


def chain_hash(prev_hash: str, event_id: str, payload_hash: str) -> str:
    return sha256_hex(f"{prev_hash}|{event_id}|{payload_hash}")


def build_raw_event(capture: RawCapture, schema_version: str = "1.0") -> RawEvent:
    """Create Contract A from a capture. Header fields are read without
    validation; a malformed message still becomes a raw event."""
    payload = capture.raw_payload
    payload_hash = sha256_hex(payload)
    message = parse_fix(payload)
    seq = message.seq_num
    source_ts = None
    source_ns = None
    sending = message.get(52)
    if sending is not None:
        try:
            source_ts, source_ns = parse_fix_timestamp(sending)
        except TimestampError:
            source_ts, source_ns = None, None
    event_id = deterministic_event_id(capture.source_system, capture.session_id, seq, payload_hash)
    return RawEvent(
        event_id=event_id,
        source_system=capture.source_system,
        session_id=capture.session_id,
        sequence_number=seq,
        received_at=capture.received_at,
        source_timestamp=source_ts,
        source_timestamp_ns=source_ns,
        protocol=message.begin_string or capture.protocol_hint,
        message_type=message.msg_type,
        raw_payload=payload,
        payload_hash=payload_hash,
        schema_version=schema_version,
    )


class RawEventStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # One connection shared across API worker threads, serialised by a lock.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- write path ---------------------------------------------------------
    def append(self, event: RawEvent) -> AppendResult:
        with self._lock:
            return self._append(event)

    def _append(self, event: RawEvent) -> AppendResult:
        cur = self._conn.cursor()
        existing = cur.execute("SELECT payload_hash FROM raw_events WHERE event_id = ?", (event.event_id,)).fetchone()
        if existing is not None:
            if existing[0] != event.payload_hash:  # pragma: no cover - impossible with deterministic IDs
                raise IntegrityError(f"event_id collision with different payload: {event.event_id}")
            cur.execute(
                "INSERT INTO delivery_log (event_id, received_at, outcome) VALUES (?, ?, ?)",
                (event.event_id, isoformat(event.received_at), AppendOutcome.DUPLICATE_DELIVERY.value),
            )
            self._conn.commit()
            return AppendResult(AppendOutcome.DUPLICATE_DELIVERY, self.get(event.event_id))
        head = cur.execute("SELECT chain_hash FROM raw_events ORDER BY chain_index DESC LIMIT 1").fetchone()
        prev = head[0] if head else GENESIS_HASH
        cur.execute(
            """INSERT INTO raw_events (event_id, source_system, session_id, sequence_number, received_at,
                source_timestamp, source_timestamp_ns, protocol, message_type, raw_payload, payload_hash,
                schema_version, prev_chain_hash, chain_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.source_system,
                event.session_id,
                event.sequence_number,
                isoformat(event.received_at),
                isoformat(event.source_timestamp),
                event.source_timestamp_ns,
                event.protocol,
                event.message_type,
                event.raw_payload,
                event.payload_hash,
                event.schema_version,
                prev,
                chain_hash(prev, event.event_id, event.payload_hash),
            ),
        )
        cur.execute(
            "INSERT INTO delivery_log (event_id, received_at, outcome) VALUES (?, ?, ?)",
            (event.event_id, isoformat(event.received_at), AppendOutcome.STORED.value),
        )
        self._conn.commit()
        return AppendResult(AppendOutcome.STORED, event)

    # -- read path ----------------------------------------------------------
    _COLUMNS = (
        "event_id, source_system, session_id, sequence_number, received_at, source_timestamp, "
        "source_timestamp_ns, protocol, message_type, raw_payload, payload_hash, schema_version"
    )

    def _row_to_event(self, row) -> RawEvent:
        (event_id, source_system, session_id, seq, received_at, source_ts, source_ns, protocol, msg_type,
         payload, payload_hash, schema_version) = row
        # RawEvent re-verifies the payload hash: a tampered row cannot be read back.
        try:
            return RawEvent(
                event_id=event_id,
                source_system=source_system,
                session_id=session_id,
                sequence_number=seq,
                received_at=parse_iso(received_at),
                source_timestamp=parse_iso(source_ts) if source_ts else None,
                source_timestamp_ns=source_ns,
                protocol=protocol,
                message_type=msg_type,
                raw_payload=payload,
                payload_hash=payload_hash,
                schema_version=schema_version,
            )
        except ValueError as exc:
            raise IntegrityError(f"raw event {event_id} failed integrity verification: {exc}") from exc

    def get(self, event_id: str) -> RawEvent:
        with self._lock:
            row = self._conn.execute(f"SELECT {self._COLUMNS} FROM raw_events WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._row_to_event(row)

    def iter_events(self) -> Iterator[RawEvent]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {self._COLUMNS} FROM raw_events ORDER BY chain_index").fetchall()
        for row in rows:
            yield self._row_to_event(row)

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]

    def duplicate_deliveries(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM delivery_log WHERE outcome = ?", (AppendOutcome.DUPLICATE_DELIVERY.value,)
            ).fetchone()[0]

    def verify_chain(self) -> ChainVerification:
        prev = GENESIS_HASH
        count = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT chain_index, event_id, raw_payload, payload_hash, prev_chain_hash, chain_hash "
                "FROM raw_events ORDER BY chain_index"
            ).fetchall()
        for index, event_id, payload, payload_hash, prev_hash, stored_hash in rows:
            count += 1
            if sha256_hex(payload) != payload_hash:
                return ChainVerification(False, count, prev, index, "payload hash mismatch")
            if prev_hash != prev:
                return ChainVerification(False, count, prev, index, "chain link mismatch")
            expected = chain_hash(prev, event_id, payload_hash)
            if stored_hash != expected:
                return ChainVerification(False, count, prev, index, "chain hash mismatch")
            prev = stored_hash
        return ChainVerification(True, count, prev)

    def manifest(self) -> dict[str, object]:
        by_source: dict[str, int] = {}
        with self._lock:
            groups = self._conn.execute(
                "SELECT source_system, COALESCE(session_id, ''), COALESCE(message_type, '?'), COUNT(*) "
                "FROM raw_events GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
            ).fetchall()
        for source, session, msg_type, n in groups:
            by_source[f"{source}|{session}|35={msg_type}"] = n
        verification = self.verify_chain()
        return {
            "raw_event_count": self.count(),
            "duplicate_deliveries": self.duplicate_deliveries(),
            "counts_by_source_session_msgtype": by_source,
            "chain_ok": verification.ok,
            "chain_head_hash": verification.head_hash,
        }
