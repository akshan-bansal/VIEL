"""Per-session FIX sequence-number tracking (spec §3.1, §5.3, VENUE-008).

Tracks MsgSeqNum per (source_system, session_id). Detects gaps, duplicates and
resends, honours SequenceReset (35=4) gap fills and resets, and records when a
gap is later recovered by retransmission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from venue_integrity.identity.exception_queue import Finding
from venue_integrity.ingestion.fix_parser import FixMessage
from venue_integrity.schemas.enums import ExceptionType


class SequenceOutcome(StrEnum):
    IN_SEQUENCE = "IN_SEQUENCE"
    GAP = "GAP"
    GAP_RECOVERED = "GAP_RECOVERED"
    POSS_DUP = "POSS_DUP"
    DUPLICATE = "DUPLICATE"
    RESET = "RESET"
    NO_SEQUENCE = "NO_SEQUENCE"


@dataclass
class SessionState:
    expected: int | None = None
    missing: set[int] = field(default_factory=set)
    recovered: set[int] = field(default_factory=set)
    gaps: list[tuple[int, int]] = field(default_factory=list)
    seen: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class SequenceCheck:
    outcome: SequenceOutcome
    findings: tuple[Finding, ...] = ()


class SequenceMonitor:
    def __init__(self) -> None:
        self.sessions: dict[tuple[str, str | None], SessionState] = {}

    def observe(self, source_system: str, session_id: str | None, message: FixMessage, event_id: str) -> SequenceCheck:
        state = self.sessions.setdefault((source_system, session_id), SessionState())
        seq = message.seq_num
        if seq is None:
            return SequenceCheck(SequenceOutcome.NO_SEQUENCE)

        poss_dup = message.get(43) == "Y"
        msg_type = message.msg_type

        if msg_type == "A" and message.get(141) == "Y":
            state.expected = seq + 1
            state.seen = {seq}
            return SequenceCheck(SequenceOutcome.RESET)

        if msg_type == "4":
            new_seq = message.get(36)
            if new_seq is not None and new_seq.isdigit():
                target = int(new_seq)
                gap_fill = message.get(123) == "Y"
                if gap_fill:
                    for n in range(seq, target):
                        state.missing.discard(n)
                        state.seen.add(n)
                if state.expected is None or target > state.expected:
                    state.expected = target
                return SequenceCheck(SequenceOutcome.RESET)

        if state.expected is None:
            state.expected = seq + 1
            state.seen.add(seq)
            return SequenceCheck(SequenceOutcome.IN_SEQUENCE)

        if seq == state.expected:
            state.expected += 1
            state.seen.add(seq)
            return SequenceCheck(SequenceOutcome.IN_SEQUENCE)

        if seq > state.expected:
            gap = (state.expected, seq - 1)
            state.gaps.append(gap)
            state.missing.update(range(gap[0], gap[1] + 1))
            state.expected = seq + 1
            state.seen.add(seq)
            return SequenceCheck(
                SequenceOutcome.GAP,
                (
                    Finding(
                        ExceptionType.SEQUENCE_GAP,
                        f"session {session_id}: expected {gap[0]}, received {seq}; missing {gap[0]}..{gap[1]}",
                        source_event_id=event_id,
                        entity_key=f"{source_system}|{session_id}|{gap[0]}-{gap[1]}",
                        context={"gap_start": gap[0], "gap_end": gap[1]},
                    ),
                ),
            )

        # seq < expected
        if seq in state.missing:
            state.missing.discard(seq)
            state.recovered.add(seq)
            state.seen.add(seq)
            return SequenceCheck(SequenceOutcome.GAP_RECOVERED)
        if poss_dup:
            return SequenceCheck(SequenceOutcome.POSS_DUP)
        return SequenceCheck(
            SequenceOutcome.DUPLICATE,
            (
                Finding(
                    ExceptionType.SEQUENCE_DUPLICATE,
                    f"session {session_id}: sequence {seq} re-used without PossDupFlag",
                    source_event_id=event_id,
                    entity_key=f"{source_system}|{session_id}|{seq}",
                ),
            ),
        )

    def open_gaps(self) -> dict[str, list[int]]:
        return {
            f"{src}|{sess}": sorted(state.missing)
            for (src, sess), state in sorted(self.sessions.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))
            if state.missing
        }

    def summary(self) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        for (src, sess), state in sorted(self.sessions.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            out[f"{src}|{sess}"] = {
                "next_expected": state.expected,
                "gaps_detected": [list(g) for g in state.gaps],
                "still_missing": sorted(state.missing),
                "recovered": sorted(state.recovered),
            }
        return out
