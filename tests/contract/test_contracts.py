"""Contract tests: spec §3 examples validate, closed-world models, invariants enforced."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from venue_integrity.schemas import (
    CandidateFeatures,
    ExecutionEvent,
    NormalizedOrder,
    RawEvent,
    RouterDecision,
    VenueIntelligence,
    VenueRecord,
)
from venue_integrity.schemas.common import sha256_hex

T = datetime(2026, 11, 23, 14, 30, tzinfo=UTC)


def test_contract_a_raw_event():
    payload = "8=FIX.4.4|9=5|35=8|10=000|"
    event = RawEvent(event_id="uuid", source_system="BROKER_A", session_id="FIX_SESSION_01", sequence_number=12881,
                     received_at="2026-11-23T14:30:00.123456Z", source_timestamp="2026-11-23T14:30:00.123400Z",
                     protocol="FIX.4.4", message_type="8", raw_payload=payload, payload_hash=sha256_hex(payload),
                     schema_version="1.0")
    assert event.received_at.microsecond == 123456  # precision preserved
    with pytest.raises(ValidationError):
        RawEvent(**{**event.model_dump(), "unexpected": 1})
    with pytest.raises(ValidationError):
        RawEvent(**{**event.model_dump(), "received_at": "2026-11-23T14:30:00"})  # naive timestamp rejected
    with pytest.raises(ValidationError):
        event.raw_payload = "x"  # immutable


def test_contract_b_venue_identity():
    record = VenueRecord(venue_id="VENUE_C", mic="XABC", raw_aliases=("ABC", "ABC_ROUTER"),
                         venue_name="Example Execution Venue", venue_type="EXCHANGE", country="CA",
                         valid_from="2026-01-01T00:00:00Z", valid_to=None, mapping_version="2.1", status="ACTIVE",
                         approved_by="REFERENCE_DATA_CONTROL")
    assert record.is_effective(T)
    with pytest.raises(ValidationError):
        VenueRecord(**{**record.model_dump(), "mic": "abc"})
    with pytest.raises(ValidationError):
        VenueRecord(**{**record.model_dump(), "valid_to": "2025-01-01T00:00:00Z"})


ORDER = dict(event_id="uuid", source_system="BROKER_A", event_time=T, cl_ord_id="CLIENT_ORDER_001",
             parent_order_id="PARENT_001", child_order_id="CHILD_001", instrument_id="INSTRUMENT_001", side="BUY",
             order_quantity=Decimal(500), raw_ord_type="1", order_type="MARKET", time_in_force="DAY",
             intended_venue_id="VENUE_A", raw_ex_destination="ABC", classification="MARKET", mapping_status="MAPPED",
             mapping_reason="MAPPED", mapping_version="2.1", normalized_at=T, normalizer_version="1.0.0")


def test_contract_c_normalized_order():
    NormalizedOrder(**ORDER)
    unmapped = {**ORDER, "intended_venue_id": None, "mapping_status": "UNMAPPED", "raw_ex_destination": "ABC_ROUTER"}
    assert NormalizedOrder(**unmapped).classification.value == "MARKET"  # unknown venue, still MARKET
    for bad in ({"classification": "OTHER"}, {"classification": "LEGACY"}, {"intended_venue_id": None},
                {"mapping_status": "UNMAPPED"}, {"actual_venue_id": "VENUE_C"}):
        with pytest.raises(ValidationError):
            NormalizedOrder(**{**ORDER, **bad})


EXEC = dict(exec_id="EXEC_001", exec_type="TRADE", raw_exec_type="F", source_event_id="uuid", source_system="BROKER_A",
            child_order_id="CHILD_001", parent_order_id="PARENT_001", instrument_id="INSTRUMENT_001", side="BUY",
            raw_last_mkt="XABC", actual_venue_id="VENUE_C", venue_mic="XABC", mapping_status="MAPPED",
            mapping_reason="MIC:MAPPED", mapping_version="2.1", executed_quantity=Decimal(500),
            execution_price=Decimal("101.25"), execution_timestamp="2026-11-23T14:30:00.125000Z",
            execution_timestamp_ns=0, payload_hash="0" * 64)


def test_contract_d_execution_event():
    ExecutionEvent(**EXEC)
    ExecutionEvent(**{**EXEC, "actual_venue_id": None, "venue_mic": None, "raw_last_mkt": None, "mapping_status": "UNMAPPED"})
    for bad in ({"actual_venue_id": None}, {"mapping_status": "UNMAPPED"}, {"executed_quantity": Decimal(0)},
                {"execution_price": None}, {"exec_type": "TRADE_CANCEL"}):
        with pytest.raises(ValidationError):
            ExecutionEvent(**{**EXEC, **bad})


def features(**kw):
    return CandidateFeatures(**{"fill_probability": 0.91, "liquidity": 0.8, "price_score": 0.9,
                                "adverse_selection_bps": 1.0, "market_impact_bps": 0.5, "transaction_cost_bps": 1.4,
                                "graph_influence": 0.73, "feature_timestamp": T, "feature_model_version": "1.4", **kw})


def test_contract_e_router_decision():
    d = RouterDecision(decision_id="DECISION_001", parent_order_id="PARENT_001", decision_timestamp=T,
                       instrument_id="INSTRUMENT_001", side="BUY", order_type="MARKET", quantity=Decimal(500),
                       candidate_venues=("VENUE_A", "VENUE_B", "VENUE_C"),
                       excluded_venues=({"venue_id": "VENUE_D", "reason": "ORDER_TYPE_MARKET_NOT_SUPPORTED"},),
                       decision_features={v: features() for v in ("VENUE_A", "VENUE_B", "VENUE_C")},
                       selected_venue="VENUE_C", router_score=0.4, router_model_version="1.4",
                       reference_data_version="2.1", decision_reason="HIGHEST_EXPECTED_UTILITY")
    assert d.decision_features["VENUE_C"].graph_influence == 0.73
    with pytest.raises(ValidationError):
        RouterDecision(**{**d.model_dump(), "selected_venue": "VENUE_Z"})
    with pytest.raises(ValidationError):
        RouterDecision(**{**d.model_dump(), "candidate_venues": ()})
    with pytest.raises(ValidationError):
        RouterDecision(**{**d.model_dump(), "excluded_venues": ({"venue_id": "VENUE_A", "reason": "x"},)})
    with pytest.raises(ValidationError):
        features(fill_probability=1.5)


def test_contract_f_venue_intelligence():
    v = VenueIntelligence(venue_id="VENUE_C", calculation_timestamp=T, window_start=T, window_end=T, graph_influence=0.73,
                          lead_lag_score=0.81, liquidity_score=0.84, adverse_selection_score=0.68, market_impact_bps=1.9,
                          latency_risk=0.55, data_quality_score=0.99, market_regime="HIGH_VOLATILITY", observations=100,
                          confidence=0.87, model_version="1.4")
    assert v.model_dump(mode="json")["market_regime"] == "HIGH_VOLATILITY"
    with pytest.raises(ValidationError):
        VenueIntelligence(**{**v.model_dump(), "data_quality_score": 1.2})
