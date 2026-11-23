"""Ground-truth counterfactual outcome model for shadow evaluation (simulation only).

For a child order sent to venue v in regime r:

    filled share     = fill probability of v
    execution cost   = half-spread_v + temp impact_v * qty/1000 + fee_v + leakage * AS_v,r
    opportunity cost = (1 - filled share) * AS_v,r / 2        (price drifts away from unfilled shares)
    shortfall        = filled share * execution cost + opportunity cost + common noise

Noise uses common random numbers: one draw per decision shared by every venue
(identical market snapshot) plus a small venue-specific draw, both seeded by
a stable hash so results do not depend on evaluation order.
"""

from __future__ import annotations

import hashlib

import numpy as np

from venue_integrity.router.shadow_evaluation import SimOutcome
from venue_integrity.schemas.router_decision import RouterDecision
from venue_integrity.simulation.scenario import ScenarioConfig


def _stable_normal(*keys: str) -> float:
    digest = hashlib.sha256("|".join(keys).encode()).digest()
    seed = int.from_bytes(digest[:8], "big")
    return float(np.random.default_rng(seed).normal())


class SimulatedOutcomeModel:
    def __init__(self, config: ScenarioConfig, noise_bps: float = 1.0, venue_noise_bps: float = 0.3):
        self.cfg = config
        self.noise_bps = noise_bps
        self.venue_noise_bps = venue_noise_bps

    def outcome(self, decision: RouterDecision, venue_id: str, regime: str) -> SimOutcome:
        v = self.cfg.venue(venue_id)
        qty = float(decision.quantity)
        adverse = v.adverse_selection_bps[regime]
        impact = v.temp_impact_bps_per_1000 * qty / 1000
        fill = v.fill_probability
        if decision.order_type.value == "LIMIT":
            fill = v.fill_probability * 0.6
            execution = -v.half_spread_bps + v.fee_bps + self.cfg.leakage_factor * adverse
            es = -2 * v.half_spread_bps
            impact = 0.0
        else:
            execution = v.half_spread_bps + impact + v.fee_bps + self.cfg.leakage_factor * adverse
            es = 2 * (v.half_spread_bps + impact)
        opportunity = (1 - fill) * adverse / 2
        noise = (self.noise_bps * _stable_normal("decision", decision.decision_id)
                 + self.venue_noise_bps * _stable_normal("venue", decision.decision_id, venue_id))
        latency = v.ack_latency_ms * float(np.exp(0.3 * _stable_normal("latency", decision.decision_id, venue_id)))
        return SimOutcome(
            fill_ratio=fill,
            shortfall_bps=fill * execution + opportunity + noise,
            effective_spread_bps=es,
            realized_spread_bps=es - adverse,
            adverse_selection_bps=adverse,
            impact_bps=impact,
            opportunity_cost_bps=opportunity,
            latency_ms=latency,
        )
