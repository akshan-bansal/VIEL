
from helpers import capture, exec_msg, order_msg

from venue_integrity.ingestion.event_validator import ValidationStatus, validate_message
from venue_integrity.ingestion.fix_parser import build_fix, parse_fix
from venue_integrity.ingestion.raw_event_store import build_raw_event
from venue_integrity.schemas.enums import ExceptionType


def validate(payload, dictionary, source="BROKER_A", offset_ms=2, **kw):
    raw = build_raw_event(capture(payload, source=source, offset_ms=offset_ms))
    return validate_message(parse_fix(payload), raw, dictionary, **kw)


def types(result):
    return {f.exception_type for f in result.findings}


def test_valid_order(dictionary):
    assert validate(order_msg("C1"), dictionary).status is ValidationStatus.VALID


def test_missing_order_type(dictionary):
    res = validate(order_msg("C1", ord_type=None), dictionary)
    assert ExceptionType.MISSING_ORDER_TYPE in types(res)
    assert res.is_usable


def test_unknown_enum_values(dictionary):
    res = validate(order_msg("C1", ord_type="Z", side="9"), dictionary)
    assert {ExceptionType.UNKNOWN_ORDER_TYPE_CODE, ExceptionType.INVALID_SIDE} <= types(res)


def test_unknown_tag_flagged_but_custom_tag_known_for_its_broker(dictionary):
    res = validate(order_msg("C1", extra=[(7777, "x")]), dictionary)
    assert ExceptionType.UNKNOWN_FIX_TAG in types(res)
    # 9001 is a BROKER_A custom tag; for an undeclared broker it is unknown
    assert ExceptionType.UNKNOWN_FIX_TAG not in types(validate(order_msg("C1"), dictionary))
    assert ExceptionType.UNKNOWN_FIX_TAG in types(validate(order_msg("C1"), dictionary, source="BROKER_Z"))


def test_clock_drift(dictionary):
    late = validate(order_msg("C1"), dictionary, offset_ms=5000)
    assert ExceptionType.CLOCK_DRIFT in types(late)
    future = validate(order_msg("C1", offset_ms=3000), dictionary, offset_ms=0)
    assert ExceptionType.CLOCK_DRIFT in types(future)


def test_invalid_timestamp(dictionary):
    payload = build_fix([(35, "D"), (49, "A"), (56, "B"), (34, "1"), (52, "2026-11-05 14:30"), (11, "C1"), (54, "1"),
                         (55, "X"), (38, "1"), (40, "1"), (60, "20261340-99:00:00")])
    res = validate(payload, dictionary)
    assert sum(1 for f in res.findings if f.exception_type is ExceptionType.INVALID_TIMESTAMP) == 2


def test_structural_failure_is_invalid(dictionary):
    payload = order_msg("C1")
    head, _, _ = payload.rpartition("10=")
    res = validate(head + "10=999\x01", dictionary)
    assert res.status is ValidationStatus.INVALID
    assert ExceptionType.PROTOCOL_VALIDATION_FAILURE in types(res)


def test_unsupported_protocol(dictionary):
    res = validate(order_msg("C1", begin="FIX.5.0SP2"), dictionary)
    assert res.status is ValidationStatus.INVALID


def test_fix42_requires_exec_trans_type(dictionary):
    res = validate(exec_msg("E1", "C1", exec_type="1", begin="FIX.4.2"), dictionary)
    assert ExceptionType.PROTOCOL_VALIDATION_FAILURE in types(res)
    ok = validate(exec_msg("E1", "C1", exec_type="1", begin="FIX.4.2", exec_trans_type="0"), dictionary)
    assert ExceptionType.PROTOCOL_VALIDATION_FAILURE not in types(ok)
