import pytest

from venue_integrity.ingestion.fix_parser import SOH, build_fix, compute_checksum, parse_fix


def sample(delim=SOH):
    return build_fix([(35, "D"), (49, "A"), (56, "B"), (34, "1"), (52, "20261105-14:30:00.000"), (11, "C1"), (40, "1")],
                     delimiter=delim)


def test_round_trip():
    m = parse_fix(sample())
    assert m.is_structurally_valid
    assert m.msg_type == "D" and m.get(11) == "C1" and m.seq_num == 1
    assert m.tags()[:3] == [8, 9, 35] and m.tags()[-1] == 10


def test_pipe_delimiter_accepted():
    m = parse_fix(sample("|"))
    assert m.is_structurally_valid and m.delimiter == "|"


def test_checksum_mismatch_detected():
    payload = sample()
    head, _, _ = payload.rpartition("10=")
    m = parse_fix(head + "10=000" + SOH)
    assert any(i.code == "CHECKSUM_MISMATCH" for i in m.issues)


def test_body_length_mismatch_detected():
    payload = sample().replace("11=C1", "11=C12")
    codes = {i.code for i in parse_fix(payload).issues}
    assert "BODY_LENGTH_MISMATCH" in codes and "CHECKSUM_MISMATCH" in codes


def test_header_order_and_missing_checksum():
    m = parse_fix(f"35=D{SOH}8=FIX.4.4{SOH}9=5{SOH}")
    codes = {i.code for i in m.issues}
    assert {"HEADER_ORDER", "MISSING_CHECKSUM"} <= codes


def test_malformed_and_duplicate_tags_reported_not_dropped():
    payload = build_fix([(35, "D"), (11, "C1"), (11, "C2"), (7777, "x")])
    m = parse_fix(payload)
    assert any(i.code == "DUPLICATE_TAG" for i in m.issues)
    assert m.get_all(11) == ["C1", "C2"]
    assert m.get(7777) == "x"
    bad = parse_fix(f"8=FIX.4.4{SOH}garbage{SOH}")
    assert any(i.code == "MALFORMED_FIELD" for i in bad.issues)


def test_empty():
    assert parse_fix("").issues[0].code == "EMPTY"


def test_build_requires_msgtype_first():
    with pytest.raises(ValueError):
        build_fix([(11, "x")])


def test_checksum_function():
    assert compute_checksum(b"\x01" * 3) == "003"
