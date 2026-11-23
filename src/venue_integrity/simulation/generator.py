"""Deterministic synthetic market, order flow, FIX traffic and SOR decisions.

Simulation mode only. The generator produces exactly what a replay of
production capture would provide — raw FIX bytes with receipt timestamps,
market data and recorded router decisions — plus the ground truth needed to
validate the audit.

Price model (per session, 100 ms grid):
    efficient log price  = random walk (regime volatility) + cumulative impact of our fills
    venue v midpoint     = efficient price lagged by v's quote lag, plus small noise
    consolidated quote   = best bid / best offer across lit venues
    graph-engine feed    = venue midpoint additionally delayed by v's feed delay

Each of our fills on venue v moves the efficient price by s * AS_v,regime / 2
bps from the next tick (permanent impact / information leakage), so the
measured adverse selection is venue- and regime-specific by construction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np

from venue_integrity.ingestion.fix_parser import SOH, build_fix
from venue_integrity.router.venue_scoring import RouterModel, score_candidates, select_venue
from venue_integrity.schemas.enums import OrderType, Side
from venue_integrity.schemas.raw_event import RawCapture
from venue_integrity.schemas.router_decision import CandidateFeatures, ExcludedVenue, RouterDecision
from venue_integrity.simulation.scenario import FaultPlan, ScenarioConfig, VenueTruth
from venue_integrity.tca.markout import MarketData, QuoteSeries
from venue_integrity.timeutil import format_fix_timestamp, to_epoch_ns

FIRM = "FIRM_OMS"
PRODUCTION_GRAPH_INFLUENCE = {"VENUE_A": 0.3, "VENUE_B": 0.1, "VENUE_C": 1.0, "VENUE_D": 0.2}


@dataclass
class PendingMessage:
    source_system: str
    session_id: str
    begin_string: str
    msg_type: str
    sender: str
    target: str
    sending_time: datetime
    receive_delay_ms: float
    body: list[tuple[int, str]]
    meta: dict[str, Any] = field(default_factory=dict)
    seq: int | None = None

    def build(self, *, poss_dup_of: datetime | None = None, sending_time: datetime | None = None) -> str:
        header = [(35, self.msg_type), (49, self.sender), (56, self.target), (34, str(self.seq))]
        if poss_dup_of is not None:
            header.append((43, "Y"))
        header.append((52, format_fix_timestamp(sending_time or self.sending_time)))
        if poss_dup_of is not None:
            header.append((122, format_fix_timestamp(poss_dup_of)))
        return build_fix(header + self.body, begin_string=self.begin_string)

    @property
    def received_at(self) -> datetime:
        return self.sending_time + timedelta(microseconds=int(self.receive_delay_ms * 1000))


@dataclass
class SimulationBundle:
    config: ScenarioConfig
    faults: FaultPlan
    captures: list[RawCapture]
    market_data: MarketData
    decisions: list[RouterDecision]
    decision_session: dict[str, int]
    session_windows: list[tuple[int, int]]
    change_events: list[tuple[datetime, str]]
    ground_truth: dict[str, Any]


def _px(value: float) -> str:
    return f"{value:.4f}"


def _qty(value: int | Decimal) -> str:
    return str(int(value))


class _Session:
    """State for one simulated trading session."""

    def __init__(self, gen: ScenarioGenerator, index: int, open_log_price: float):
        self.gen = gen
        self.cfg = gen.cfg
        self.index = index
        self.rng = gen.rng
        self.start = self.cfg.session_start(index)
        self.T = self.cfg.ticks_per_session
        self.regime = self.cfg.regime_of_session(index)
        sigma = self.cfg.tick_vol_bps[self.regime] / 1e4
        self.base = open_log_price + np.cumsum(self.rng.normal(0.0, sigma, self.T))
        self.impulse = np.zeros(self.T + 1)
        self.noise = {v.venue_id: self.rng.normal(0.0, self.cfg.venue_noise_bps / 1e4, self.T) for v in self.cfg.venues}

    def tick_of(self, t: datetime) -> int:
        ms = (t - self.start).total_seconds() * 1000
        return int(min(self.T - 1, max(0, ms // self.cfg.tick_ms)))

    def eff_log(self, tick: int) -> float:
        tick = max(0, tick)
        return float(self.base[tick] + self.impulse[: tick + 1].sum())

    def venue_mid(self, venue: VenueTruth, tick: int) -> float:
        return math.exp(self.eff_log(tick - venue.quote_lag_ticks) + self.noise[venue.venue_id][tick])

    def venue_quote(self, venue: VenueTruth, tick: int) -> tuple[float, float, float]:
        mid = self.venue_mid(venue, tick)
        hs = mid * venue.half_spread_bps / 1e4
        return mid - hs, mid + hs, mid

    def nbbo(self, tick: int) -> tuple[float, float]:
        """Best bid / offer across lit venues. A crossed book is assumed to be swept
        by arbitrage immediately, leaving the leading venue's quote as the touch."""
        lit = [v for v in self.cfg.venues if v.half_spread_bps > 0]
        quotes = [self.venue_quote(v, tick) for v in lit]
        bid, ask = max(q[0] for q in quotes), min(q[1] for q in quotes)
        if bid > ask:
            leader = min(lit, key=lambda v: v.quote_lag_ticks)
            bid, ask, _ = self.venue_quote(leader, tick)
        return bid, ask


class ScenarioGenerator:
    def __init__(self, config: ScenarioConfig | None = None, faults: FaultPlan | None = None,
                 router_models: dict[str, RouterModel] | None = None):
        self.cfg = config or ScenarioConfig()
        self.faults = faults or FaultPlan()
        self.rng = np.random.default_rng(self.cfg.seed)
        if router_models is None:
            from venue_integrity.config import load_config
            from venue_integrity.router.venue_scoring import load_router_models

            router_models = load_router_models(load_config().model_weights)
        self.models = router_models
        self.messages: list[PendingMessage] = []
        self.decisions: list[RouterDecision] = []
        self.decision_session: dict[str, int] = {}
        self.truth_fills: list[dict[str, Any]] = []
        self.ack_latency: dict[str, list[float]] = {v.venue_id: [] for v in self.cfg.venues}
        self.order_counter = 0
        self.exec_counter = 0
        self.planted: dict[str, list[Any]] = {k: [] for k in (
            "possdup_exec_ids", "exact_redelivery_seq", "corrected_exec_ids", "busted_exec_ids",
            "missing_lastmkt_exec_ids", "expired_mic_exec_ids", "unknown_tag_exec_ids",
            "missing_ord_type_orders", "corrupt_checksum", "orphan_exec_ids", "lost_exec_ids",
            "recovered_exec_ids", "clock_drift_exec_ids", "lookahead_decisions", "stale_weight_decisions",
            "fallback_decisions", "legacy_alias_orders", "ambiguous_alias_orders")}

    # ------------------------------------------------------------------ helpers
    def _next_exec_id(self, broker: str) -> str:
        self.exec_counter += 1
        return f"{broker[-1]}EX{self.exec_counter:06d}"

    def _alias_for(self, broker: str, venue_id: str, session: int) -> str:
        venue = self.cfg.venue(venue_id)
        if broker == "BROKER_A":
            if venue_id == "VENUE_C" and session >= self.cfg.alias_change_session:
                return self.cfg.new_alias
            return venue.alias
        if venue_id == "VENUE_B":
            return "B_ROUTE_7"
        if venue_id == "VENUE_A" and self.rng.random() < 0.5:
            return "SMART"
        return venue.alias

    def _production_features(self, s: _Session, venue: VenueTruth, side: Side, qty: int, order_type: OrderType,
                             tick: int, decision_time: datetime) -> CandidateFeatures:
        bid, ask, mid = s.venue_quote(venue, tick)
        nb, na = s.nbbo(tick)
        nmid = (nb + na) / 2
        if venue.half_spread_bps == 0:
            price_score = 1.0
        else:
            touch, ntouch = (ask, na) if side is Side.BUY else (bid, nb)
            gap_bps = abs(touch - ntouch) / nmid * 1e4
            price_score = max(0.0, min(1.0, 1.0 - gap_bps / 2.0))
        as_prod = venue.adverse_selection_bps["NORMAL"] + float(self.rng.normal(0, 0.05))
        if venue.venue_id == "VENUE_C" and s.index >= self.cfg.alias_change_session:
            as_prod = self.cfg.production_c_as_after_defect_bps + float(self.rng.normal(0, 0.05))
        depth = venue.depth_shares * float(self.rng.uniform(0.7, 1.3))
        return CandidateFeatures(
            fill_probability=round(venue.fill_probability, 4),
            liquidity=round(min(1.0, depth / qty), 6),
            price_score=round(price_score, 6),
            adverse_selection_bps=round(as_prod, 6),
            market_impact_bps=round(venue.temp_impact_bps_per_1000 * qty / 1000, 6),
            transaction_cost_bps=round(venue.half_spread_bps + venue.fee_bps, 6),
            graph_influence=PRODUCTION_GRAPH_INFLUENCE[venue.venue_id],
            feature_timestamp=decision_time - timedelta(seconds=1),
            feature_model_version="1.0",
        )

    # ------------------------------------------------------------------ session
    def _run_session(self, index: int, open_log_price: float) -> tuple[_Session, float]:
        cfg = self.cfg
        s = _Session(self, index, open_log_price)
        parents = []
        n_parents = cfg.parents_per_session
        arrivals = np.sort(self.rng.uniform(5.0, cfg.session_seconds - 60.0, n_parents))
        for p in range(n_parents):
            broker = "BROKER_B" if self.rng.random() < cfg.broker_b_share else "BROKER_A"
            side = Side.BUY if self.rng.random() < 0.5 else Side.SELL
            otype = OrderType.LIMIT if self.rng.random() < cfg.limit_order_share else OrderType.MARKET
            n_children = int(self.rng.integers(2, 5))
            t = float(arrivals[p])
            children = []
            for c in range(n_children):
                qty = int(self.rng.integers(2, 16)) * 100
                children.append((t, c, qty))
                t += float(self.rng.uniform(1.0, 4.0))
            parents.append({"pid": f"S{index:02d}-P{p:03d}", "client": f"CL-{int(self.rng.integers(1, 9)):02d}",
                            "broker": broker, "side": side, "otype": otype, "children": children})
        events = sorted(((t, p_i, c, qty) for p_i, par in enumerate(parents) for t, c, qty in par["children"]),
                        key=lambda e: (e[0], e[1], e[2]))
        for t, p_i, c, qty in events:
            self._child(s, parents[p_i], c, qty, s.start + timedelta(seconds=t))
        close_log = float(s.base[-1] + s.impulse[: s.T].sum())
        return s, close_log

    def _child(self, s: _Session, parent: dict, c: int, qty: int, t: datetime) -> None:
        cfg = self.cfg
        broker, side, otype = parent["broker"], parent["side"], parent["otype"]
        begin = "FIX.4.4" if broker == "BROKER_A" else "FIX.4.2"
        cl_ord_id = f"{parent['pid']}-C{c}"
        decision_id = f"DEC-{cl_ord_id}"
        tick = s.tick_of(t)
        candidates = [v for v in cfg.venues if otype.value in v.order_types]
        excluded = [ExcludedVenue(venue_id=v.venue_id, reason=f"ORDER_TYPE_{otype.value}_NOT_SUPPORTED")
                    for v in cfg.venues if otype.value not in v.order_types]
        features = {v.venue_id: self._production_features(s, v, side, qty, otype, tick, t) for v in candidates}
        fallbacks: list[str] = []
        model_version = cfg.router_model_version
        scoring_model = self.models[model_version]
        n_dec = len(self.decisions)
        if n_dec in (40, 400, 1200)[: self.faults.feature_fallback_decisions]:
            victim = sorted(features)[0]
            features[victim] = features[victim].model_copy(update={"adverse_selection_bps": 5.0, "graph_influence": None})
            fallbacks.append(f"{victim}:adverse_selection_bps=APPROVED_DEFAULT_5.0")
            self.planted["fallback_decisions"].append(decision_id)
        if n_dec in (777,)[: self.faults.lookahead_decisions]:
            victim = sorted(features)[-1]
            features[victim] = features[victim].model_copy(update={"feature_timestamp": t + timedelta(seconds=5)})
            self.planted["lookahead_decisions"].append(decision_id)
        if n_dec in (901,)[: self.faults.stale_weight_decisions]:
            scoring_model = self.models["1.3"]  # router silently ran stale weights but recorded 1.4
            self.planted["stale_weight_decisions"].append(decision_id)
        scores = score_candidates(features, scoring_model)
        venue_id = select_venue(scores, scoring_model)
        decision = RouterDecision(
            decision_id=decision_id,
            parent_order_id=parent["pid"],
            child_order_id=cl_ord_id,
            decision_timestamp=t,
            instrument_id=cfg.instrument_id,
            side=side,
            order_type=otype,
            quantity=Decimal(qty),
            candidate_venues=tuple(sorted(features)),
            excluded_venues=tuple(excluded),
            decision_features=features,
            selected_venue=venue_id,
            router_score=scores[venue_id],
            router_model_version=model_version,
            reference_data_version=cfg.reference_data_version,
            decision_reason="HIGHEST_EXPECTED_UTILITY",
            fallbacks_applied=tuple(fallbacks),
        )
        self.decisions.append(decision)
        self.decision_session[decision_id] = s.index
        venue = cfg.venue(venue_id)
        alias = self._alias_for(broker, venue_id, s.index)
        if alias == cfg.new_alias:
            self.planted["legacy_alias_orders"].append(cl_ord_id)
        if alias == "SMART":
            self.planted["ambiguous_alias_orders"].append(cl_ord_id)

        # ---- NewOrderSingle -------------------------------------------------
        date = s.start.strftime("%Y%m%d")
        ord_session = f"{broker}_ORD_{date}"
        exec_session = f"{broker}_EXEC_{date}"
        bid, ask, _ = s.venue_quote(venue, tick)
        limit_px = None
        if otype is OrderType.LIMIT:
            nb, na = s.nbbo(tick)
            limit_px = round(nb if side is Side.BUY else na, 4)
        body = [(11, cl_ord_id), (583, parent["pid"]), (9001, parent["client"]), (1, "ACCT-001"),
                (55, cfg.instrument_id), (54, "1" if side is Side.BUY else "2"), (38, _qty(qty))]
        missing_ord_type = (len(self.planted["missing_ord_type_orders"]) < self.faults.missing_ord_type_orders
                            and s.index == 2 and otype is OrderType.MARKET)
        if missing_ord_type:
            self.planted["missing_ord_type_orders"].append(cl_ord_id)
        else:
            body.append((40, "1" if otype is OrderType.MARKET else "2"))
        if limit_px is not None:
            body.append((44, _px(limit_px)))
        body += [(59, "3" if otype is OrderType.MARKET else "0"), (60, format_fix_timestamp(t)), (100, alias)]
        self.messages.append(PendingMessage(broker, ord_session, begin, "D", FIRM, broker, t,
                                            float(self.rng.uniform(0.2, 1.0)), body, {"cl_ord_id": cl_ord_id}))

        # ---- broker / venue behaviour --------------------------------------
        broker_order_id = f"{broker[-1]}ORD{self.order_counter:06d}"
        self.order_counter += 1
        latency = venue.ack_latency_ms * float(self.rng.lognormal(0.0, 0.3))
        self.ack_latency[venue_id].append(latency)
        t_ack = t + timedelta(microseconds=int(latency * 1000))
        drift = (broker == "BROKER_B" and s.index == self.faults.clock_drift_session
                 and 120 <= (t - s.start).total_seconds() <= 420)

        def exec_report(when: datetime, fields: list[tuple[int, str]], meta: dict) -> PendingMessage:
            stamp = when - timedelta(seconds=3) if drift else when
            delay = float(self.rng.uniform(0.2, 1.5)) + (3000.0 if drift else 0.0)
            common = [(37, broker_order_id), (11, cl_ord_id), (583, parent["pid"]), (55, cfg.instrument_id),
                      (54, "1" if side is Side.BUY else "2"), (38, _qty(qty))]
            body = common + fields + [(60, format_fix_timestamp(stamp))]
            msg = PendingMessage(broker, exec_session, begin, "8", broker, FIRM, stamp, delay, body,
                                 {**meta, "true_time": when})
            if drift:
                self.planted["clock_drift_exec_ids"].append(meta.get("exec_id"))
            self.messages.append(msg)
            return msg

        def exec_type_fields(kind: str) -> list[tuple[int, str]]:
            # kind: NEW / FILL / PARTIAL / CANCELED
            if begin == "FIX.4.4":
                mapping = {"NEW": ("0", "0"), "PARTIAL": ("F", "1"), "FILL": ("F", "2"), "CANCELED": ("4", "4")}
                et, os_ = mapping[kind]
                return [(150, et), (39, os_)]
            mapping = {"NEW": ("0", "0"), "PARTIAL": ("1", "1"), "FILL": ("2", "2"), "CANCELED": ("4", "4")}
            et, os_ = mapping[kind]
            return [(20, "0"), (150, et), (39, os_)]

        ack_id = self._next_exec_id(broker)
        exec_report(t_ack, [(17, ack_id)] + exec_type_fields("NEW") +
                    [(14, "0"), (151, _qty(qty)), (6, "0")], {"exec_id": ack_id, "kind": "NEW"})

        fill_prob = venue.fill_probability if otype is OrderType.MARKET else venue.fill_probability * 0.6
        if otype is OrderType.MARKET:
            filled_total = qty if self.rng.random() < fill_prob else int(qty * self.rng.uniform(0.4, 0.9)) // 100 * 100
        else:
            filled_total = int(qty * self.rng.uniform(0.3, 1.0)) // 100 * 100 if self.rng.random() < fill_prob else 0
        n_fills = 0 if filled_total == 0 else min(int(self.rng.integers(1, 4)), filled_total // 100)
        sizes = []
        if n_fills:
            cuts = sorted(self.rng.choice(np.arange(1, filled_total // 100), size=n_fills - 1, replace=False)) if n_fills > 1 else []
            bounds = [0, *[int(x) * 100 for x in cuts], filled_total]
            sizes = [bounds[i + 1] - bounds[i] for i in range(n_fills)]
        cum = 0
        notional = 0.0
        t_fill = t_ack
        regime = s.regime
        sgn = side.direction
        for size in sizes:
            if otype is OrderType.MARKET:
                t_fill = t_fill + timedelta(microseconds=int(self.rng.uniform(0.5, 3.0) * 1000))
            else:
                t_fill = t_fill + timedelta(milliseconds=int(self.rng.uniform(500, 3000)))
            ftick = s.tick_of(t_fill)
            fbid, fask, fmid = s.venue_quote(venue, ftick)
            if venue.half_spread_bps == 0:
                nb, na = s.nbbo(ftick)
                price = (nb + na) / 2
            elif otype is OrderType.LIMIT:
                price = limit_px
            else:
                touch = fask if sgn > 0 else fbid
                price = touch * (1 + sgn * venue.temp_impact_bps_per_1000 * (cum + size) / 1000 / 1e4)
            price = round(price, 4)
            cum += size
            notional += size * price
            exec_id = self._next_exec_id(broker)
            kind = "FILL" if cum == qty else "PARTIAL"
            fields = [(17, exec_id)] + exec_type_fields(kind) + [(32, _qty(size)), (31, _px(price))]
            last_mkt: str | None = venue.mic
            if self.rng.random() < self.faults.missing_lastmkt_rate:
                last_mkt = None
                self.planted["missing_lastmkt_exec_ids"].append(exec_id)
            elif len(self.planted["expired_mic_exec_ids"]) < self.faults.expired_mic_fills and s.index == 4:
                last_mkt = "XVNX"
                self.planted["expired_mic_exec_ids"].append(exec_id)
            if last_mkt:
                fields.append((30, last_mkt))
            fields += [(14, _qty(cum)), (151, _qty(qty - cum)), (6, _px(notional / cum))]
            if broker == "BROKER_A" and len(self.planted["unknown_tag_exec_ids"]) < self.faults.unknown_tag_messages \
                    and s.index == 3:
                fields.append((7777, "VENDOR_X"))
                self.planted["unknown_tag_exec_ids"].append(exec_id)
            exec_report(t_fill, fields, {"exec_id": exec_id, "kind": "TRADE", "qty": size, "price": price,
                                         "cum": cum, "notional": notional, "last_mkt": last_mkt,
                                         "child": cl_ord_id, "venue": venue_id, "order_qty": qty,
                                         "otype": otype.value})
            self.truth_fills.append({"exec_id": exec_id, "venue": venue_id, "session": s.index, "qty": size,
                                     "child": cl_ord_id, "broker": broker})
        if cum:
            # permanent impact / information leakage of the child order, from the tick after its
            # last fill: every fill's markout then measures AS_v,regime (= 2 x the mid move)
            s.impulse[min(s.tick_of(t_fill) + 1, s.T)] += sgn * venue.adverse_selection_bps[regime] / 2 / 1e4
        if cum < qty:
            t_cxl = t_fill + timedelta(milliseconds=1 if otype is OrderType.MARKET else 0)
            if otype is OrderType.LIMIT:
                t_cxl = s.start + timedelta(seconds=cfg.session_seconds - 1)
            cxl_id = self._next_exec_id(broker)
            exec_report(t_cxl, [(17, cxl_id)] + exec_type_fields("CANCELED") +
                        [(14, _qty(cum)), (151, "0"), (6, _px(notional / cum) if cum else "0")],
                        {"exec_id": cxl_id, "kind": "CANCELED"})

    # ------------------------------------------------------------------ faults
    def _assign_sequences(self) -> None:
        streams: dict[tuple[str, str], list[PendingMessage]] = {}
        for m in self.messages:
            streams.setdefault((m.source_system, m.session_id), []).append(m)
        for key in sorted(streams):
            # sequence numbers follow the true send order, not a (possibly drifting) clock
            ordered = sorted(streams[key], key=lambda m: (m.meta.get("true_time", m.sending_time),
                                                          m.meta.get("exec_id") or m.meta.get("cl_ord_id") or ""))
            last_received = None
            for i, m in enumerate(ordered, start=1):
                m.seq = i
                # one TCP session: messages arrive in sequence order
                if last_received is not None and m.received_at <= last_received:
                    m.receive_delay_ms = ((last_received - m.sending_time).total_seconds() * 1000) + 0.01
                last_received = m.received_at

    def _plant_trade_modifications(self) -> None:
        """Corrections and busts, appended to the same broker sessions."""
        # market-order fills only: their child has no further reports after the +30 s modification,
        # so every later broker CumQty stays consistent with the corrected state
        fills = [m for m in self.messages if m.meta.get("kind") == "TRADE" and m.meta.get("last_mkt")
                 and m.meta.get("otype") == "MARKET"]
        a_fills = [m for m in fills if m.source_system == "BROKER_A"]
        b_fills = [m for m in fills if m.source_system == "BROKER_B"]
        picks_a = [a_fills[i] for i in np.linspace(10, len(a_fills) - 50, self.faults.corrections_44 + self.faults.busts_44).astype(int)]
        picks_b = [b_fills[i] for i in np.linspace(5, len(b_fills) - 20, self.faults.corrections_42 + self.faults.busts_42).astype(int)]
        plan = [(m, "CORRECT") for m in picks_a[: self.faults.corrections_44]]
        plan += [(m, "BUST") for m in picks_a[self.faults.corrections_44:]]
        plan += [(m, "CORRECT") for m in picks_b[: self.faults.corrections_42]]
        plan += [(m, "BUST") for m in picks_b[self.faults.corrections_42:]]
        for original, action in plan:
            meta = original.meta
            when = original.sending_time + timedelta(seconds=30)
            exec_id = self._next_exec_id(original.source_system)
            body_index = dict(original.body)
            # final cumulative quantity reported for the child before this modification
            child_cum = max(m.meta["cum"] for m in self.messages
                            if m.meta.get("kind") == "TRADE" and m.meta.get("child") == meta["child"]
                            and m.source_system == original.source_system)
            if action == "CORRECT":
                new_qty = meta["qty"] - 100 if meta["qty"] > 100 else meta["qty"]
                new_px = round(meta["price"] + 0.0005 * (1 if meta["qty"] <= 100 else 0), 4)
                new_cum = child_cum - (meta["qty"] - new_qty)
                fields_qty = [(32, _qty(new_qty)), (31, _px(new_px)), (30, meta["last_mkt"])]
                self.planted["corrected_exec_ids"].append(meta["exec_id"])
            else:
                new_cum = child_cum - meta["qty"]
                fields_qty = [(32, _qty(meta["qty"])), (31, _px(meta["price"])), (30, meta["last_mkt"])]
                self.planted["busted_exec_ids"].append(meta["exec_id"])
            if original.begin_string == "FIX.4.4":
                type_fields = [(150, "G" if action == "CORRECT" else "H"), (39, "1" if new_cum < meta["order_qty"] else "2")]
            else:
                type_fields = [(20, "2" if action == "CORRECT" else "1"), (150, body_index[150]),
                               (39, "1" if new_cum < meta["order_qty"] else "2")]
            common = [(37, body_index[37]), (11, body_index[11]), (583, body_index[583]), (55, body_index[55]),
                      (54, body_index[54]), (38, body_index[38])]
            body = common + [(17, exec_id), (19, meta["exec_id"])] + type_fields + fields_qty + \
                [(14, _qty(new_cum)), (151, _qty(meta["order_qty"] - new_cum)), (60, format_fix_timestamp(when))]
            self.messages.append(PendingMessage(original.source_system, original.session_id, original.begin_string, "8",
                                                original.sender, original.target, when, 1.0, body,
                                                {"exec_id": exec_id, "kind": action, "ref": meta["exec_id"]}))

    def _plant_orphan(self) -> None:
        sample = next(m for m in self.messages if m.meta.get("kind") == "TRADE" and m.source_system == "BROKER_A")
        for i in range(self.faults.orphan_executions):
            exec_id = self._next_exec_id("BROKER_A")
            body = [(37, "AORD-UNKNOWN"), (11, f"UNKNOWN-CHILD-{i}"), (55, self.cfg.instrument_id), (54, "1"),
                    (38, "100"), (17, exec_id), (150, "F"), (39, "2"), (32, "100"),
                    (31, dict(sample.body)[31]), (30, "XVNA"), (14, "100"), (151, "0"),
                    (60, format_fix_timestamp(sample.sending_time + timedelta(seconds=7)))]
            self.messages.append(PendingMessage("BROKER_A", sample.session_id, "FIX.4.4", "8", "BROKER_A", FIRM,
                                                sample.sending_time + timedelta(seconds=7), 0.8, body,
                                                {"exec_id": exec_id, "kind": "ORPHAN"}))
            self.planted["orphan_exec_ids"].append(exec_id)

    def _captures(self) -> list[RawCapture]:
        self._assign_sequences()
        captures: list[tuple[datetime, int, RawCapture]] = []
        order = 0

        def add(received: datetime, capture: RawCapture) -> None:
            nonlocal order
            captures.append((received, order, capture))
            order += 1

        lost_done = recovered_done = 0
        possdup_targets = set()
        trade_msgs = [m for m in self.messages if m.meta.get("kind") == "TRADE" and m.source_system == "BROKER_A"]
        for i in np.linspace(100, len(trade_msgs) - 100, self.faults.possdup_resends).astype(int):
            possdup_targets.add(id(trade_msgs[i]))
        redeliver_targets = {id(m) for m in self.messages[200:200 + 3 * self.faults.exact_redeliveries:3]}
        corrupt_target = next(id(m) for m in self.messages
                              if m.meta.get("kind") == "NEW" and m.source_system == "BROKER_A" and m.seq and m.seq > 20)
        for m in self.messages:
            session_index = self._session_index(m.sending_time)
            payload = m.build()
            exec_id = m.meta.get("exec_id")
            if (m.meta.get("kind") == "TRADE" and m.source_system == "BROKER_A"
                    and session_index == self.faults.lost_message_session and not lost_done):
                lost_done = 1
                self.planted["lost_exec_ids"].append(exec_id)
                continue  # message never delivered -> sequence gap, missing fill
            if (m.meta.get("kind") == "TRADE" and m.source_system == "BROKER_A"
                    and session_index == self.faults.recovered_gap_session and not recovered_done):
                recovered_done = 1
                self.planted["recovered_exec_ids"].append(exec_id)
                resend_time = m.sending_time + timedelta(seconds=10)  # after later messages: gap, then recovery
                resent = m.build(poss_dup_of=m.sending_time, sending_time=resend_time)
                add(resend_time + timedelta(milliseconds=1), RawCapture(source_system=m.source_system,
                    session_id=m.session_id, received_at=resend_time + timedelta(milliseconds=1), raw_payload=resent))
                continue
            if id(m) == corrupt_target:
                head, _, _ = payload.rpartition("10=")
                payload = head + "10=000" + SOH
                self.planted["corrupt_checksum"].append(m.meta.get("exec_id"))
            capture = RawCapture(source_system=m.source_system, session_id=m.session_id,
                                 received_at=m.received_at, raw_payload=payload)
            add(m.received_at, capture)
            if id(m) in possdup_targets:
                resend_time = m.sending_time + timedelta(seconds=1)
                resent = m.build(poss_dup_of=m.sending_time, sending_time=resend_time)
                add(resend_time + timedelta(milliseconds=1), RawCapture(source_system=m.source_system,
                    session_id=m.session_id, received_at=resend_time + timedelta(milliseconds=1), raw_payload=resent))
                self.planted["possdup_exec_ids"].append(exec_id)
            if id(m) in redeliver_targets:
                again = m.received_at + timedelta(milliseconds=50)
                add(again, RawCapture(source_system=m.source_system, session_id=m.session_id,
                                      received_at=again, raw_payload=payload))
                self.planted["exact_redelivery_seq"].append(f"{m.session_id}:{m.seq}")
        captures.sort(key=lambda x: (x[0], x[1]))
        return [c for _, _, c in captures]

    def _session_index(self, t: datetime) -> int:
        for i, (start, end) in enumerate(self._windows):
            if start <= to_epoch_ns(t) <= end + 5 * 60 * 10**9:
                return i
        return -1

    # ------------------------------------------------------------------ main
    def generate(self) -> SimulationBundle:
        cfg = self.cfg
        log_price = math.log(cfg.start_price)
        sessions: list[_Session] = []
        self._windows: list[tuple[int, int]] = []
        for i in range(cfg.sessions):
            s, log_price = self._run_session(i, log_price)
            sessions.append(s)
            start_ns = to_epoch_ns(s.start)
            self._windows.append((start_ns, start_ns + (s.T - 1) * cfg.tick_ms * 1_000_000))
        self._plant_trade_modifications()
        self._plant_orphan()
        captures = self._captures()
        market_data = self._market_data(sessions)
        venue_truth = {v.venue_id: v.__dict__ for v in cfg.venues}
        change_events = [
            (cfg.session_start(cfg.alias_change_session) - timedelta(hours=12),
             f"BROKER_A routing configuration release: VENUE_C destination alias changed to {cfg.new_alias}"),
        ]
        ground_truth = {
            "venues": venue_truth,
            "leader": "VENUE_C",
            "alias_change_session": cfg.alias_change_session,
            "new_alias": cfg.new_alias,
            "session_regimes": {i: cfg.regime_of_session(i) for i in range(cfg.sessions)},
            "planted_faults": {k: list(v) for k, v in self.planted.items()},
            "true_fills": len(self.truth_fills),
        }
        return SimulationBundle(cfg, self.faults, captures, market_data, self.decisions, self.decision_session,
                                list(self._windows), change_events, ground_truth)

    def _market_data(self, sessions: list[_Session]) -> MarketData:
        cfg = self.cfg
        tick_ns = cfg.tick_ms * 1_000_000
        ts_all, bid_all, ask_all, sess_all = [], [], [], []
        grid: dict[str, list[np.ndarray]] = {v.venue_id: [] for v in cfg.venues}
        windows = []
        lit = [v for v in cfg.venues if v.half_spread_bps > 0]
        for s in sessions:
            eff = s.base + np.cumsum(s.impulse[: s.T])
            start_ns = to_epoch_ns(s.start)
            ts = start_ns + np.arange(s.T, dtype=np.int64) * tick_ns
            mids = {}
            for v in cfg.venues:
                idx = np.clip(np.arange(s.T) - v.quote_lag_ticks, 0, None)
                mids[v.venue_id] = np.exp(eff[idx] + s.noise[v.venue_id])
            bids = np.max([mids[v.venue_id] * (1 - v.half_spread_bps / 1e4) for v in lit], axis=0)
            asks = np.min([mids[v.venue_id] * (1 + v.half_spread_bps / 1e4) for v in lit], axis=0)
            leader = min(lit, key=lambda v: v.quote_lag_ticks)
            crossed = bids > asks  # swept by arbitrage: leader's quote is the touch (see _Session.nbbo)
            bids = np.where(crossed, mids[leader.venue_id] * (1 - leader.half_spread_bps / 1e4), bids)
            asks = np.where(crossed, mids[leader.venue_id] * (1 + leader.half_spread_bps / 1e4), asks)
            keep = np.ones(s.T, dtype=bool)
            if s.index == self.faults.stale_quote_session:
                lo, hi = self.faults.stale_quote_window_s
                keep &= ~((np.arange(s.T) * cfg.tick_ms / 1000 >= lo) & (np.arange(s.T) * cfg.tick_ms / 1000 < hi))
            ts_all.append(ts[keep])
            bid_all.append(bids[keep])
            ask_all.append(asks[keep])
            windows.append((int(ts[0]), int(ts[-1])))
            for v in cfg.venues:
                observed = mids[v.venue_id]
                if v.feed_delay_ticks:
                    idx = np.clip(np.arange(s.T) - v.feed_delay_ticks, 0, None)
                    observed = observed[idx]
                observed = observed.copy()
                if v.venue_id == "VENUE_D":
                    holes = self.rng.random(s.T) < self.faults.missing_grid_share_venue_d
                    observed[holes] = np.nan
                grid[v.venue_id].append(observed)
            sess_all.append(np.full(s.T, s.index))
        consolidated = QuoteSeries(np.concatenate(ts_all), np.concatenate(bid_all), np.concatenate(ask_all), windows)
        grid_ts = np.concatenate([start + np.arange(cfg.ticks_per_session, dtype=np.int64) * tick_ns
                                  for start, _ in windows])
        return MarketData(
            consolidated={cfg.instrument_id: consolidated},
            grid_ts_ns=grid_ts,
            venue_mids={k: np.concatenate(v) for k, v in grid.items()},
            grid_session=np.concatenate(sess_all),
            grid_tick_ns=tick_ns,
            feed_latency_ns={v.venue_id: v.feed_delay_ticks * tick_ns for v in cfg.venues},
            ack_latency_ms={k: list(v) for k, v in self.ack_latency.items()},
        )


def generate_scenario(config: ScenarioConfig | None = None, faults: FaultPlan | None = None) -> SimulationBundle:
    return ScenarioGenerator(config, faults).generate()
