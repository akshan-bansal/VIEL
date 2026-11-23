from venue_integrity.ingestion.fix_parser import build_fix, parse_fix
from venue_integrity.ingestion.sequence_monitor import SequenceMonitor, SequenceOutcome
from venue_integrity.schemas.enums import ExceptionType


def msg(seq, msg_type="0", **tags):
    fields = [(35, msg_type), (34, str(seq))] + [(int(k[1:]), v) for k, v in tags.items()]
    return parse_fix(build_fix(fields))


def test_in_sequence_gap_recovery_and_possdup():
    mon = SequenceMonitor()
    assert mon.observe("B", "S", msg(1), "e1").outcome is SequenceOutcome.IN_SEQUENCE
    assert mon.observe("B", "S", msg(2), "e2").outcome is SequenceOutcome.IN_SEQUENCE
    gap = mon.observe("B", "S", msg(5), "e5")
    assert gap.outcome is SequenceOutcome.GAP
    assert gap.findings[0].exception_type is ExceptionType.SEQUENCE_GAP
    assert mon.open_gaps() == {"B|S": [3, 4]}
    assert mon.observe("B", "S", msg(3, t43="Y"), "e3").outcome is SequenceOutcome.GAP_RECOVERED
    assert mon.open_gaps() == {"B|S": [4]}
    assert mon.observe("B", "S", msg(2, t43="Y"), "e2b").outcome is SequenceOutcome.POSS_DUP
    dup = mon.observe("B", "S", msg(2), "e2c")
    assert dup.outcome is SequenceOutcome.DUPLICATE
    assert dup.findings[0].exception_type is ExceptionType.SEQUENCE_DUPLICATE


def test_gap_fill_and_reset():
    mon = SequenceMonitor()
    mon.observe("B", "S", msg(1), "e1")
    mon.observe("B", "S", msg(4), "e4")  # gap 2..3
    mon.observe("B", "S", msg(2, msg_type="4", t36="4", t123="Y"), "gf")  # gap fill covers 2..3
    assert mon.open_gaps() == {}
    assert mon.observe("B", "S", msg(1, msg_type="A", t141="Y"), "logon").outcome is SequenceOutcome.RESET
    assert mon.observe("B", "S", msg(2), "e").outcome is SequenceOutcome.IN_SEQUENCE


def test_sessions_are_independent():
    mon = SequenceMonitor()
    mon.observe("B", "S1", msg(1), "a")
    assert mon.observe("B", "S2", msg(7), "b").outcome is SequenceOutcome.IN_SEQUENCE
