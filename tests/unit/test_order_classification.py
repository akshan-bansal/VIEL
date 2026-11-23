"""Spec §11.2 critical unit tests and the Classification ⊥ Venue invariant (§5.2)."""

import inspect

import pytest

from venue_integrity import (
    Classification,
    MappingStatus,
    OrderType,
    classify_order,
    normalize_order_type,
    resolve_venue,
)

REGISTRY = {"ABC": [{"venue_id": "VENUE_A", "mic": "XABC"}]}


def test_market_order_remains_market():
    result = normalize_order_type("1")
    assert result == OrderType.MARKET
    assert classify_order(result) == Classification.MARKET


def test_unknown_venue_not_legacy():
    result = resolve_venue("UNKNOWN_VENUE", REGISTRY, "1.0")
    assert result.mapping_status == MappingStatus.UNMAPPED
    assert result.venue_id is None
    # the order's classification is unaffected by the failed lookup
    assert classify_order(normalize_order_type("1")) == Classification.MARKET


def test_known_alias_maps():
    result = resolve_venue("ABC", REGISTRY, "1.0")
    assert result.mapping_status is MappingStatus.MAPPED
    assert result.venue_id == "VENUE_A"
    assert result.mapping_version == "1.0"


@pytest.mark.parametrize(
    "code, expected",
    [("1", OrderType.MARKET), ("2", OrderType.LIMIT), ("3", OrderType.STOP), ("4", OrderType.OTHER),
     ("K", OrderType.OTHER), ("P", OrderType.OTHER), (None, OrderType.UNKNOWN), ("", OrderType.UNKNOWN),
     ("Z", OrderType.UNKNOWN), ("market", OrderType.UNKNOWN), (" 1", OrderType.UNKNOWN)],
)
def test_order_type_codes(code, expected):
    assert normalize_order_type(code) is expected


def test_unknown_order_type_is_unresolved_not_market_or_other():
    assert classify_order(OrderType.UNKNOWN) is Classification.UNRESOLVED


def test_classification_has_no_legacy_member():
    assert "LEGACY" not in Classification.__members__
    with pytest.raises(ValueError):
        Classification("LEGACY")


def test_classifier_cannot_receive_a_venue():
    # Structural guarantee: neither function accepts any venue input.
    assert list(inspect.signature(classify_order).parameters) == ["order_type"]
    assert list(inspect.signature(normalize_order_type).parameters) == ["raw_ord_type"]


def test_classify_rejects_non_order_type():
    with pytest.raises(ValueError):
        classify_order("LEGACY")
