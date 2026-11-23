from datetime import UTC, datetime

import pytest

from venue_integrity.router.venue_scoring import (
    WEIGHTS,
    RouterModel,
    VenueCandidate,
    apply_hard_constraints,
    calculate_venue_utility,
    load_router_models,
    score_candidates,
    select_venue,
)
from venue_integrity.schemas.enums import MappingStatus, OrderType
from venue_integrity.schemas.router_decision import CandidateFeatures

T = datetime(2026, 11, 5, tzinfo=UTC)


def feats(**kw):
    base = dict(fill_probability=0.9, liquidity=1.0, price_score=1.0, adverse_selection_bps=1.0, market_impact_bps=0.5,
                transaction_cost_bps=1.5, graph_influence=0.5, feature_timestamp=T, feature_model_version="1.0")
    return CandidateFeatures(**{**base, **kw})


def test_utility_formula():
    v = VenueCandidate("A", 0.9, 1.0, 1.0, 1.0, 0.5, 1.5)
    expected = 0.25 * 0.9 + 0.2 + 0.2 - 0.15 * 1.0 / 5 - 0.1 * 0.5 / 5 - 0.1 * 1.5 / 5
    assert calculate_venue_utility(v) == pytest.approx(expected)
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_models_load_and_versions_differ(config):
    models = load_router_models(config.model_weights)
    assert set(models) == {"1.3", "1.4"}
    v = VenueCandidate("A", 0.9, 1.0, 1.0, 3.0, 0.5, 1.5)
    assert calculate_venue_utility(v, models["1.3"]) != calculate_venue_utility(v, models["1.4"])


def test_selection_tie_break_and_missing_features(config):
    model = load_router_models(config.model_weights)["1.4"]
    scores = score_candidates({"B": feats(), "A": feats(), "C": feats(adverse_selection_bps=None)}, model)
    assert scores["C"] is None
    assert select_venue(scores, model) == "A"  # equal scores -> ascending venue id
    assert select_venue({"A": None}, model) is None


def test_scale_independence_of_other_candidates(config):
    model = load_router_models(config.model_weights)["1.4"]
    alone = score_candidates({"A": feats()}, model)["A"]
    together = score_candidates({"A": feats(), "B": feats(adverse_selection_bps=9.0)}, model)["A"]
    assert alone == together


def test_hard_constraints_before_scoring():
    eligible, excluded = apply_hard_constraints(
        ["VENUE_A", "VENUE_C", "VENUE_D", "VENUE_X"], order_type=OrderType.MARKET,
        identity_status={"VENUE_A": MappingStatus.MAPPED, "VENUE_C": MappingStatus.MAPPED,
                         "VENUE_D": MappingStatus.MAPPED, "VENUE_X": MappingStatus.INVALID},
        venue_order_types={"VENUE_A": ["MARKET"], "VENUE_C": ["MARKET"], "VENUE_D": ["LIMIT"]},
        client_excluded={"VENUE_C"})
    assert eligible == ["VENUE_A"]
    assert {e.venue_id: e.reason for e in excluded} == {
        "VENUE_C": "CLIENT_INSTRUCTION_EXCLUDED", "VENUE_D": "ORDER_TYPE_MARKET_NOT_SUPPORTED",
        "VENUE_X": "IDENTITY_INVALID"}


def test_router_model_validation():
    with pytest.raises(ValueError):
        RouterModel("x", {"fill": 1.0}, {"adverse_selection": 1, "market_impact": 1, "transaction_cost": 1})
