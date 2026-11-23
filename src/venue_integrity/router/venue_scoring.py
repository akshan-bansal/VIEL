"""Candidate venue scoring (spec §10.1–10.2).

    U_v = w1 F_v + w2 L_v + w3 P_v - w4 AS_v/s_AS - w5 MI_v/s_MI - w6 TC_v/s_TC

F, L, P are dimensionless in [0, 1]; AS, MI, TC are in bps and divided by a
fixed, versioned scale so every term is dimensionless. Fixed scales (rather
than min-max across the candidates of one decision) keep a venue's utility
independent of which other venues happen to be eligible.

Hard constraints (identity, order-type support, client exclusions) are applied
BEFORE scoring and every exclusion carries a reason. Weights are illustrative,
versioned parameters — not production values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from venue_integrity.schemas.enums import MappingStatus, OrderType
from venue_integrity.schemas.router_decision import CandidateFeatures, ExcludedVenue


@dataclass
class VenueCandidate:
    venue_id: str
    fill_probability: float
    liquidity: float
    price_score: float
    adverse_selection: float
    market_impact: float
    transaction_cost: float


WEIGHTS = {"fill": 0.25, "liquidity": 0.20, "price": 0.20, "adverse": 0.15, "impact": 0.10, "cost": 0.10}
DEFAULT_SCALES = {"adverse_selection": 5.0, "market_impact": 5.0, "transaction_cost": 5.0}


@dataclass(frozen=True)
class RouterModel:
    version: str
    weights: dict[str, float]
    scales_bps: dict[str, float]
    tie_break: str = "venue_id_ascending"

    def __post_init__(self) -> None:
        missing = set(WEIGHTS) - set(self.weights)
        if missing:
            raise ValueError(f"router model {self.version} missing weights {sorted(missing)}")
        if any(v <= 0 for v in self.scales_bps.values()):
            raise ValueError("scales must be positive")


def load_router_models(model_weights: dict[str, Any]) -> dict[str, RouterModel]:
    return {
        str(version): RouterModel(
            version=str(version),
            weights={k: float(v) for k, v in spec["weights"].items()},
            scales_bps={k: float(v) for k, v in spec["scales_bps"].items()},
            tie_break=spec.get("tie_break", "venue_id_ascending"),
        )
        for version, spec in model_weights["router_models"].items()
    }


def calculate_venue_utility(v: VenueCandidate, model: RouterModel | None = None) -> float:
    weights = model.weights if model else WEIGHTS
    scales = model.scales_bps if model else DEFAULT_SCALES
    components = [
        v.fill_probability,
        v.liquidity,
        v.price_score,
        v.adverse_selection,
        v.market_impact,
        v.transaction_cost,
    ]
    for value in components:
        if value is None:
            raise ValueError(f"missing feature for {v.venue_id}")
    return (
        weights["fill"] * v.fill_probability
        + weights["liquidity"] * v.liquidity
        + weights["price"] * v.price_score
        - weights["adverse"] * v.adverse_selection / scales["adverse_selection"]
        - weights["impact"] * v.market_impact / scales["market_impact"]
        - weights["cost"] * v.transaction_cost / scales["transaction_cost"]
    )


REQUIRED_FEATURES = ("fill_probability", "liquidity", "price_score", "adverse_selection_bps",
                     "market_impact_bps", "transaction_cost_bps")


def missing_features(features: CandidateFeatures) -> list[str]:
    return [name for name in REQUIRED_FEATURES if getattr(features, name) is None]


def candidate_from_features(venue_id: str, features: CandidateFeatures) -> VenueCandidate:
    missing = missing_features(features)
    if missing:
        raise ValueError(f"{venue_id}: missing {missing}")
    return VenueCandidate(
        venue_id=venue_id,
        fill_probability=features.fill_probability,
        liquidity=features.liquidity,
        price_score=features.price_score,
        adverse_selection=features.adverse_selection_bps,
        market_impact=features.market_impact_bps,
        transaction_cost=features.transaction_cost_bps,
    )


def score_candidates(features: dict[str, CandidateFeatures], model: RouterModel) -> dict[str, float | None]:
    scores: dict[str, float | None] = {}
    for venue in sorted(features):
        f = features[venue]
        scores[venue] = None if missing_features(f) else calculate_venue_utility(candidate_from_features(venue, f), model)
    return scores


def select_venue(scores: dict[str, float | None], model: RouterModel) -> str | None:
    scored = [(v, s) for v, s in scores.items() if s is not None]
    if not scored:
        return None
    if model.tie_break != "venue_id_ascending":
        raise ValueError(f"unsupported tie_break {model.tie_break}")
    best = max(s for _, s in scored)
    return sorted(v for v, s in scored if s == best)[0]


def apply_hard_constraints(
    venues: list[str],
    *,
    order_type: OrderType,
    identity_status: dict[str, MappingStatus],
    venue_order_types: dict[str, list[str]],
    client_excluded: set[str] | None = None,
) -> tuple[list[str], list[ExcludedVenue]]:
    eligible: list[str] = []
    excluded: list[ExcludedVenue] = []
    client_excluded = client_excluded or set()
    for venue in sorted(venues):
        status = identity_status.get(venue, MappingStatus.UNMAPPED)
        if status is not MappingStatus.MAPPED:
            excluded.append(ExcludedVenue(venue_id=venue, reason=f"IDENTITY_{status.value}"))
        elif venue in client_excluded:
            excluded.append(ExcludedVenue(venue_id=venue, reason="CLIENT_INSTRUCTION_EXCLUDED"))
        elif order_type.value not in venue_order_types.get(venue, []):
            excluded.append(ExcludedVenue(venue_id=venue, reason=f"ORDER_TYPE_{order_type.value}_NOT_SUPPORTED"))
        else:
            eligible.append(venue)
    return eligible, excluded
