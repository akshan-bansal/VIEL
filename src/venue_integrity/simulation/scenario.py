"""Synthetic audit scenario: ground truth and fault-injection plan.

Everything here is ILLUSTRATIVE. The scenario exists to exercise the audit end
to end in simulation mode, with known ground truth to validate against.

Story
-----
* VENUE_C is the price leader (its quotes move first); A, B and D follow with
  lags. VENUE_B's market-data *feed* is additionally delayed, which a naive
  lead-lag estimate would misread as VENUE_B following everyone.
* From session ``alias_change_session`` BROKER_A's routing configuration starts
  using a new destination alias, ``ABC_ROUTER``, for VENUE_C. Registry v2.0
  does not know it. The legacy normaliser labels those market orders LEGACY,
  and the production venue analytics lose most of VENUE_C's flow from their
  venue statistics. The production SOR's adverse-selection feature for VENUE_C
  is *modelled* as collapsing to ``production_c_as_after_defect_bps`` from that
  session (a synthetic assumption about the feature feed), so the router sends
  VENUE_C even more market-order flow. The audit has to detect this from the
  recorded decision features, not from the assumption.
* A planted set of protocol and reconciliation faults covers the spec §11.4
  failure-injection matrix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class VenueTruth:
    venue_id: str
    mic: str
    alias: str
    quote_lag_ticks: int
    feed_delay_ticks: int
    half_spread_bps: float
    temp_impact_bps_per_1000: float
    fee_bps: float
    adverse_selection_bps: dict[str, float]  # by regime: LOW_VOLATILITY / NORMAL / HIGH_VOLATILITY
    fill_probability: float
    depth_shares: int
    ack_latency_ms: float
    order_types: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioConfig:
    seed: int = 20261123
    first_session: datetime = datetime(2026, 10, 26, 14, 30, tzinfo=UTC)
    sessions: int = 20
    session_seconds: int = 600
    tick_ms: int = 100
    instrument_id: str = "INSTRUMENT_001"
    start_price: float = 100.0
    parents_per_session: int = 30
    broker_b_share: float = 0.2
    limit_order_share: float = 0.2
    alias_change_session: int = 8
    new_alias: str = "ABC_ROUTER"
    new_alias_valid_from: datetime = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    high_vol_sessions: tuple[int, ...] = (3, 9, 12, 15, 18)
    low_vol_sessions: tuple[int, ...] = (1, 6, 10, 13, 16)
    tick_vol_bps: dict[str, float] = field(
        default_factory=lambda: {"LOW_VOLATILITY": 0.25, "NORMAL": 0.5, "HIGH_VOLATILITY": 1.0}
    )
    venue_noise_bps: float = 0.1
    leakage_factor: float = 0.5
    router_model_version: str = "1.4"
    reference_data_version: str = "2.0"
    # production analytics view of VENUE_C's adverse selection once its flow is mis-attributed
    production_c_as_after_defect_bps: float = 0.6
    venues: tuple[VenueTruth, ...] = (
        VenueTruth("VENUE_A", "XVNA", "VNA", 1, 0, 1.2, 0.5, 0.30,
                   {"LOW_VOLATILITY": 0.8, "NORMAL": 1.2, "HIGH_VOLATILITY": 1.8}, 0.95, 2500, 3.0,
                   ("MARKET", "LIMIT", "STOP", "OTHER")),
        VenueTruth("VENUE_B", "XVNB", "VNB", 2, 1, 1.4, 0.7, 0.25,
                   {"LOW_VOLATILITY": 0.6, "NORMAL": 1.0, "HIGH_VOLATILITY": 1.4}, 0.93, 2000, 4.0,
                   ("MARKET", "LIMIT", "STOP", "OTHER")),
        VenueTruth("VENUE_C", "XABC", "ABC", 0, 0, 1.0, 0.6, 0.35,
                   {"LOW_VOLATILITY": 1.2, "NORMAL": 2.4, "HIGH_VOLATILITY": 5.0}, 0.97, 3500, 2.0,
                   ("MARKET", "LIMIT", "OTHER")),
        VenueTruth("VENUE_D", "XVND", "VND", 1, 0, 0.0, 0.0, 0.10,
                   {"LOW_VOLATILITY": 1.0, "NORMAL": 1.6, "HIGH_VOLATILITY": 2.4}, 0.55, 1500, 6.0,
                   ("LIMIT",)),
    )

    def venue(self, venue_id: str) -> VenueTruth:
        return next(v for v in self.venues if v.venue_id == venue_id)

    def session_start(self, index: int) -> datetime:
        """Business days starting at ``first_session`` (weekends skipped)."""
        day = self.first_session
        count = 0
        while True:
            if day.weekday() < 5:
                if count == index:
                    return day
                count += 1
            day += timedelta(days=1)

    def regime_of_session(self, index: int) -> str:
        if index in self.high_vol_sessions:
            return "HIGH_VOLATILITY"
        if index in self.low_vol_sessions:
            return "LOW_VOLATILITY"
        return "NORMAL"

    @property
    def ticks_per_session(self) -> int:
        return self.session_seconds * 1000 // self.tick_ms


@dataclass(frozen=True)
class FaultPlan:
    """Deterministic fault injection (spec §11.4). Indices are generation order."""

    possdup_resends: int = 5  # exec reports re-sent with PossDupFlag=Y (same ExecID)
    exact_redeliveries: int = 3  # identical bytes delivered twice (idempotent ingest)
    corrections_44: int = 4  # ExecType=G
    corrections_42: int = 2  # ExecTransType=2
    busts_44: int = 3  # ExecType=H
    busts_42: int = 1  # ExecTransType=1
    missing_lastmkt_rate: float = 0.01
    expired_mic_fills: int = 1  # LastMkt = MIC with EXPIRED status
    unknown_tag_messages: int = 4
    missing_ord_type_orders: int = 1
    corrupt_checksum_messages: int = 1
    orphan_executions: int = 1
    lost_message_session: int = 11  # exec report lost (sequence gap, never recovered)
    recovered_gap_session: int = 5  # exec report delayed and re-sent (gap recovered)
    clock_drift_session: int = 7  # broker B SendingTime lagging receipt by 3 s for a burst
    stale_quote_session: int = 14  # consolidated quotes missing for a window
    stale_quote_window_s: tuple[float, float] = (300.0, 302.5)
    missing_grid_share_venue_d: float = 0.05
    lookahead_decisions: int = 1
    stale_weight_decisions: int = 1
    feature_fallback_decisions: int = 3
