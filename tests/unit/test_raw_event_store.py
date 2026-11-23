import sqlite3

import pytest
from helpers import capture, order_msg

from venue_integrity.ingestion.raw_event_store import AppendOutcome, IntegrityError, RawEventStore, build_raw_event
from venue_integrity.schemas.common import sha256_hex
from venue_integrity.schemas.raw_event import RawEvent


def test_append_get_and_idempotency(tmp_path):
    store = RawEventStore(tmp_path / "raw.sqlite")
    event = build_raw_event(capture(order_msg("C1")))
    assert store.append(event).outcome is AppendOutcome.STORED
    again = build_raw_event(capture(order_msg("C1"), offset_ms=99))  # same bytes delivered later
    assert again.event_id == event.event_id
    assert store.append(again).outcome is AppendOutcome.DUPLICATE_DELIVERY
    assert store.count() == 1 and store.duplicate_deliveries() == 1
    assert store.get(event.event_id).raw_payload == event.raw_payload
    assert store.verify_chain().ok


def test_triggers_block_mutation(tmp_path):
    path = tmp_path / "raw.sqlite"
    store = RawEventStore(path)
    store.append(build_raw_event(capture(order_msg("C1"))))
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE raw_events SET raw_payload = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM raw_events")


def test_tampering_is_detected(tmp_path):
    path = tmp_path / "raw.sqlite"
    store = RawEventStore(path)
    e1 = build_raw_event(capture(order_msg("C1", seq=1)))
    e2 = build_raw_event(capture(order_msg("C2", seq=2)))
    store.append(e1)
    store.append(e2)
    # an attacker with DDL rights drops the trigger and edits a payload
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER raw_events_no_update")
    conn.execute("UPDATE raw_events SET raw_payload = replace(raw_payload, '38=500', '38=900') WHERE event_id = ?",
                 (e1.event_id,))
    conn.commit()
    verification = store.verify_chain()
    assert not verification.ok and verification.reason == "payload hash mismatch"
    with pytest.raises(IntegrityError):
        store.get(e1.event_id)


def test_chain_links_detect_reordering(tmp_path):
    path = tmp_path / "raw.sqlite"
    store = RawEventStore(path)
    for i in range(3):
        store.append(build_raw_event(capture(order_msg(f"C{i}", seq=i + 1))))
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER raw_events_no_update")
    conn.execute("UPDATE raw_events SET prev_chain_hash = ? WHERE chain_index = 2", ("0" * 64,))
    conn.commit()
    assert store.verify_chain().reason == "chain link mismatch"


def test_raw_event_rejects_hash_mismatch():
    event = build_raw_event(capture(order_msg("C1")))
    with pytest.raises(ValueError, match="payload_hash mismatch"):
        RawEvent(**{**event.model_dump(), "raw_payload": event.raw_payload + "x"})
    assert event.payload_hash == sha256_hex(event.raw_payload)


def test_malformed_message_still_stored():
    store = RawEventStore()
    result = store.append(build_raw_event(capture("not a fix message")))
    assert result.outcome is AppendOutcome.STORED and store.count() == 1
