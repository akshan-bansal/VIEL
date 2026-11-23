"""End-to-end audit pipeline (replay / simulation mode).

    captures -> raw store (immutable, hash-chained)
             -> parse / validate / sequence-check
             -> identity resolution + order normalisation (classification ⊥ venue)
             -> execution reconciliation + lineage
             -> TCA (actual-venue attribution)
             -> lead-lag graph + venue influence
             -> venue intelligence (influence and risk kept separate)
             -> contamination detection + stage-level defect localisation
             -> SOR decision reconstruction + shadow evaluation
             -> evidence package

Normalisation always reads from the raw store, so reprocessing after a
reference-data fix provably uses the original, unchanged bytes.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from venue_integrity.alerts import AlertSink
from venue_integrity.analytics.contamination_detector import (
    ChangeEvent,
    ClassifiedOrder,
    ContaminationReport,
    detect_contamination,
)
from venue_integrity.analytics.defect_localization import OrderStageRecord, localise
from venue_integrity.analytics.venue_risk import compute_venue_intelligence
from venue_integrity.config import ConfigBundle, assert_supported_mode, load_config
from venue_integrity.execution.execution_reconciler import ReconciliationReport, reconcile
from venue_integrity.execution.legacy_classifier import LegacyNormalizer, legacy_alias_table
from venue_integrity.execution.lineage import LineageIndex
from venue_integrity.execution.order_normalizer import (
    GovernedRouteRule,
    NormalizationContext,
    normalize_execution_report,
    normalize_new_order,
)
from venue_integrity.graph.centrality import InfluenceResult, calculate_influence
from venue_integrity.graph.lead_lag import LeadLagResult, estimate_lead_lag, session_returns
from venue_integrity.graph.network_builder import build_venue_graph
from venue_integrity.graph.regime_detection import classify_sessions, session_volatility
from venue_integrity.identity.exception_queue import ExceptionQueue, Finding
from venue_integrity.identity.mic_registry import MicReference, VenueRegistry
from venue_integrity.identity.venue_resolver import resolve_venue
from venue_integrity.ingestion.event_validator import ValidationStatus, validate_message
from venue_integrity.ingestion.fix_parser import FixDictionary, parse_fix
from venue_integrity.ingestion.raw_event_store import AppendOutcome, RawEventStore, build_raw_event
from venue_integrity.ingestion.sequence_monitor import SequenceMonitor
from venue_integrity.router.decision_reconstruction import ReconstructionResult, reconstruct_decision
from venue_integrity.router.shadow_evaluation import OutcomeModel, Policy, ShadowReport, evaluate_shadow
from venue_integrity.router.venue_scoring import RouterModel, load_router_models
from venue_integrity.schemas.enums import DefectCategory, IdentifierType, MappingStatus, MarketRegime
from venue_integrity.schemas.execution_event import ExecutionEvent
from venue_integrity.schemas.normalized_order import NormalizedOrder
from venue_integrity.schemas.raw_event import RawCapture
from venue_integrity.schemas.router_decision import CandidateFeatures, RouterDecision
from venue_integrity.schemas.venue_intelligence import VenueIntelligence
from venue_integrity.tca.engine import TcaEngine, TcaReport
from venue_integrity.tca.markout import MarketData
from venue_integrity.timeutil import from_epoch_ns, to_epoch_ns


@dataclass
class IngestionReport:
    captures: int = 0
    stored: int = 0
    duplicate_deliveries: int = 0
    valid: int = 0
    valid_with_warnings: int = 0
    invalid: int = 0
    sequence_outcomes: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class NormalizationResult:
    registry_version: str
    orders: list[NormalizedOrder]
    executions: list[ExecutionEvent]
    findings: list[Finding]
    stats: dict[str, Any]


@dataclass
class GraphResult:
    lead_lag: LeadLagResult
    influence: InfluenceResult
    graph: Any
    regimes: dict[int, MarketRegime]
    session_vol: dict[int, float]


class AuditPipeline:
    def __init__(
        self,
        config: ConfigBundle | None = None,
        *,
        registry: VenueRegistry | None = None,
        raw_store: RawEventStore | None = None,
        mode: str = "replay",
    ):
        assert_supported_mode(mode)
        self.mode = mode
        self.config = config or load_config()
        self.settings = self.config.settings
        self.dictionary = FixDictionary(self.config.fix_dictionary)
        self.mic_reference = MicReference.from_csv(self.config.mic_reference_path)
        self.registry = registry or VenueRegistry.from_config(self.config.venue_registry)
        self.raw_store = raw_store or RawEventStore(":memory:")
        self.alerts = AlertSink()
        self.exceptions = ExceptionQueue(self.alerts)
        self.sequence_monitor = SequenceMonitor()
        self.router_models: dict[str, RouterModel] = load_router_models(self.config.model_weights)
        self.governed_rules = tuple(GovernedRouteRule.from_config(r) for r in self.settings.governed_route_categories)
        self.ingestion = IngestionReport()
        self._validation: dict[str, ValidationStatus] = {}

    # ------------------------------------------------------------------ ingest
    def ingest(self, captures: list[RawCapture]) -> IngestionReport:
        ing = self.settings.ingestion
        report = self.ingestion
        seq_counts: dict[str, int] = defaultdict(int)
        for capture in captures:
            report.captures += 1
            event = build_raw_event(capture, schema_version=str(ing["schema_version"]))
            result = self.raw_store.append(event)
            if result.outcome is AppendOutcome.DUPLICATE_DELIVERY:
                report.duplicate_deliveries += 1
                continue
            report.stored += 1
            message = parse_fix(event.raw_payload)
            validation = validate_message(
                message, event, self.dictionary,
                clock_drift_tolerance_ms=int(ing["clock_drift_tolerance_ms"]),
                future_timestamp_tolerance_ms=int(ing["future_timestamp_tolerance_ms"]),
            )
            self._validation[event.event_id] = validation.status
            if validation.status is ValidationStatus.INVALID:
                report.invalid += 1
            elif validation.status is ValidationStatus.VALID_WITH_WARNINGS:
                report.valid_with_warnings += 1
            else:
                report.valid += 1
            check = self.sequence_monitor.observe(event.source_system, event.session_id, message, event.event_id)
            seq_counts[check.outcome.value] += 1
            self.exceptions.raise_all([*validation.findings, *check.findings], event.received_at)
        report.sequence_outcomes = dict(sorted(seq_counts.items()))
        return report

    # --------------------------------------------------------------- normalise
    def normalize(self, registry: VenueRegistry | None = None, registry_version: str | None = None,
                  raise_exceptions: bool = True) -> NormalizationResult:
        registry = registry or self.registry
        ctx = NormalizationContext(
            registry=registry,
            dictionary=self.dictionary,
            registry_version=registry_version,
            mic_reference=self.mic_reference,
            lastmkt_alias_resolution=bool(self.settings.section("identity").get("lastmkt_alias_resolution", True)),
            governed_rules=self.governed_rules,
        )
        orders: list[NormalizedOrder] = []
        executions: list[ExecutionEvent] = []
        findings: list[Finding] = []
        skipped_invalid = 0
        for event in self.raw_store.iter_events():
            status = self._validation.get(event.event_id)
            message = parse_fix(event.raw_payload)
            if status is None:
                status = validate_message(message, event, self.dictionary).status
            if status is ValidationStatus.INVALID:
                skipped_invalid += 1
                continue
            if message.msg_type == "D":
                order, f = normalize_new_order(message, event, ctx, processing_time=event.received_at)
                findings.extend(f)
                if order is not None:
                    orders.append(order)
            elif message.msg_type == "8":
                execution, f = normalize_execution_report(message, event, ctx)
                findings.extend(f)
                if execution is not None:
                    executions.append(execution)
        if raise_exceptions:
            at = max((o.normalized_at for o in orders), default=None)
            for finding in findings:
                self.exceptions.raise_finding(finding, at or datetime.fromtimestamp(0).astimezone())
        mapping = defaultdict(int)
        for o in orders:
            mapping[f"order:{o.mapping_status.value}"] += 1
        for e in executions:
            if e.exec_type.is_economic:
                mapping[f"execution:{e.mapping_status.value}"] += 1
        stats = {
            "registry_version": ctx.version,
            "orders": len(orders),
            "execution_reports": len(executions),
            "skipped_invalid_messages": skipped_invalid,
            "mapping_status_counts": dict(sorted(mapping.items())),
            "classification_counts": dict(sorted(_count(o.classification.value for o in orders).items())),
            "order_type_counts": dict(sorted(_count(o.order_type.value for o in orders).items())),
            "route_category_counts": dict(sorted(_count(str(o.route_category) for o in orders).items())),
        }
        return NormalizationResult(ctx.version, orders, executions, findings, stats)

    def reconcile(self, normalized: NormalizationResult, raise_exceptions: bool = True) -> ReconciliationReport:
        report = reconcile(normalized.orders, normalized.executions)
        if raise_exceptions:
            at = from_epoch_ns(max((e.trade_timestamp_ns for e in report.executions), default=0))
            self.exceptions.raise_all(report.findings, at)
        return report

    def lineage(self, normalized: NormalizationResult, report: ReconciliationReport) -> LineageIndex:
        return LineageIndex(normalized.orders, report, self.raw_store)

    # ---------------------------------------------------------------- analytics
    def run_tca(self, market_data: MarketData, report: ReconciliationReport, raise_exceptions: bool = True) -> TcaReport:
        tca_cfg = self.settings.tca
        engine = TcaEngine(
            market_data,
            horizons_ms=[int(h) for h in tca_cfg["horizons_ms"]],
            reference_horizon_ms=int(tca_cfg["reference_horizon_ms"]),
            max_staleness_ms=int(tca_cfg["max_midpoint_staleness_ms"]),
            fee_schedule_bps={k: float(v) for k, v in tca_cfg["fee_schedule_bps"].items()},
        )
        result = engine.run(report)
        if raise_exceptions and result.findings:
            at = from_epoch_ns(max(e.trade_timestamp_ns for e in report.executions))
            self.exceptions.raise_all(result.findings, at)
        return result

    def run_graph(self, market_data: MarketData, *, latency_adjust: bool = True,
                  session_mask: np.ndarray | None = None) -> GraphResult:
        g = self.settings.graph
        tick = market_data.grid_tick_ns
        latency = {v: int(round(ns / tick)) for v, ns in market_data.feed_latency_ns.items() if ns} if latency_adjust else {}
        mids = market_data.venue_mids
        sessions = market_data.grid_session
        if session_mask is not None:
            mids = {k: v[session_mask] for k, v in mids.items()}
            sessions = sessions[session_mask]
        lead_lag = estimate_lead_lag(
            mids, sessions,
            max_lag=int(g["max_lag"]),
            significance=float(g["significance"]),
            min_observations=int(g["min_observations"]),
            latency_adjustment_ticks=latency,
            min_net_lead=float(g.get("min_net_lead", 0.05)),
        )
        graph = build_venue_graph(lead_lag.edge_records(), lead_lag.venues)
        influence = calculate_influence(graph)
        series = next(iter(market_data.consolidated.values()))
        reg = self.settings.section("regime")
        vols = _consolidated_session_vol(series, market_data)
        regimes = classify_sessions(vols, high_percentile=float(reg["high_volatility_percentile"]),
                                    low_percentile=float(reg["low_volatility_percentile"]))
        return GraphResult(lead_lag, influence, graph, regimes, vols)

    def venue_intelligence(self, tca: TcaReport, graph: GraphResult, market_data: MarketData,
                           report: ReconciliationReport, calculated_at: datetime,
                           model_version: str = "1.0") -> tuple[list[VenueIntelligence], list[Finding]]:
        model = self.config.model_weights["venue_intelligence"][model_version]
        venues = sorted(set(self.registry.venue_ids()) | {f.venue_key for f in tca.fills if f.venue_key != "UNRESOLVED"})
        data_quality = data_quality_by_venue(report, tca)
        start = from_epoch_ns(int(market_data.grid_ts_ns[0]))
        end = from_epoch_ns(int(market_data.grid_ts_ns[-1]))
        regimes = list(graph.regimes.values())
        dominant = max(set(regimes), key=regimes.count) if regimes else MarketRegime.UNKNOWN
        intel, findings = compute_venue_intelligence(
            venues, tca=tca, influence=graph.influence, lead_lag=graph.lead_lag, graph=graph.graph,
            ack_latency_ms=market_data.ack_latency_ms, data_quality=data_quality, regime=dominant,
            window=(start, end), calculated_at=calculated_at, model=model, model_version=model_version,
        )
        self.exceptions.raise_all(findings, calculated_at)
        return intel, findings

    # ------------------------------------------------------- legacy comparison
    def legacy_stage_records(self, normalized: NormalizationResult, legacy_registry_version: str) -> list[OrderStageRecord]:
        """Run the reconstructed legacy path over the same raw events (differential audit)."""
        legacy = LegacyNormalizer(legacy_alias_table(self.registry, legacy_registry_version))
        records = []
        for order in normalized.orders:
            trace = legacy.trace(order.raw_ord_type, order.raw_ex_destination)
            records.append(OrderStageRecord(
                order_key=order.event_id,
                raw_ord_type=order.raw_ord_type,
                venue_status="MAPPED" if trace.venue_lookup is not None else "UNMAPPED",
                raw_destination=order.raw_ex_destination,
                stage_labels={
                    DefectCategory.PROTOCOL_INGESTION: trace.protocol_label if order.raw_ord_type in ("1", "2")
                    else order.classification.value,
                    DefectCategory.ORDER_TYPE_NORMALIZATION: trace.normalized_label,
                    DefectCategory.OMS_EMS_ROUTING: trace.oms_label,
                    DefectCategory.ANALYTICS_MODEL_TRAINING: trace.analytics_label,
                },
            ))
        return records

    def localise_defect(self, records: list[OrderStageRecord]) -> dict[str, Any]:
        return localise(records)

    def contamination(self, normalized: NormalizationResult, legacy_registry_version: str,
                      corrected: VenueRegistry, venue_influence: dict[str, float],
                      session_windows: list[tuple[int, int]], change_events: list[tuple[datetime, str]],
                      report: ReconciliationReport | None = None) -> ContaminationReport:
        legacy = LegacyNormalizer(legacy_alias_table(self.registry, legacy_registry_version))
        labels = {}
        for i, (start, _) in enumerate(session_windows):
            labels[i] = from_epoch_ns(start).date().isoformat()

        def bucket(t_ns: int) -> int:
            for i, (start, end) in enumerate(session_windows):
                if start - 3_600 * 10**9 <= t_ns <= end + 3_600 * 10**9:
                    return i
            return -1

        orders = []
        for o in normalized.orders:
            b = bucket(to_epoch_ns(o.event_time))
            res = resolve_venue(o.raw_ex_destination, corrected, event_time=o.event_time, source_system=o.source_system,
                                identifier_type=IdentifierType.MIC if o.raw_ex_destination_id_source == "G" else IdentifierType.ALIAS,
                                mic_reference=self.mic_reference)
            orders.append(ClassifiedOrder(
                event_id=o.event_id, bucket=b, bucket_label=labels.get(b, "?"), raw_ord_type=o.raw_ord_type,
                audited_label=legacy.classify(o.raw_ord_type, o.raw_ex_destination),
                raw_destination=o.raw_ex_destination, corrected_venue_id=res.venue_id,
                quantity=float(o.order_quantity),
            ))
        events = []
        for when, description in change_events:
            t = to_epoch_ns(when)
            following = [i for i, (start, _) in enumerate(session_windows) if start >= t]
            if following:
                events.append(ChangeEvent(following[0], description))
        pairs = None
        if report is not None:
            pairs = [(bucket(e.trade_timestamp_ns), e.intended_venue_id,
                      e.actual_venue_id if e.mapping_status is MappingStatus.MAPPED else None)
                     for e in report.executions if e.quantity > 0]
        c = self.settings.section("contamination")
        result = detect_contamination(
            orders, venue_influence=venue_influence, change_events=events, destination_pairs=pairs,
            baseline_window=int(c["baseline_window"]), zscore_threshold=float(c["zscore_threshold"]),
            cusum_k=float(c["cusum_k"]), cusum_h=float(c["cusum_h"]),
        )
        at = from_epoch_ns(session_windows[-1][1]) if session_windows else datetime.now().astimezone()
        self.exceptions.raise_all(result.findings, at)
        return result

    # ------------------------------------------------------------------ router
    def reconstruct_decisions(self, decisions: list[RouterDecision], report: ReconciliationReport | None,
                              tca: TcaReport | None) -> list[ReconstructionResult]:
        tol = float(self.settings.router.get("score_tolerance", 1e-9))
        results = []
        for d in sorted(decisions, key=lambda d: (d.decision_timestamp, d.decision_id)):
            r = reconstruct_decision(d, self.router_models, registry=self.registry, tolerance=tol,
                                     reconciliation=report, tca=tca)
            self.exceptions.raise_all(r.findings, d.decision_timestamp)
            results.append(r)
        return results

    def feature_integrity(self, decisions: list[RouterDecision], *, tca: TcaReport, graph: GraphResult,
                          market_data: MarketData, decision_session: dict[str, int]) -> dict[str, Any]:
        from venue_integrity.router.feature_audit import audit_feature_integrity

        features = PointInTimeFeatures(tca, graph, market_data, decision_session)
        summary, findings = audit_feature_integrity(decisions, features.corrected, decision_session)
        if findings:
            self.exceptions.raise_all(findings, max(d.decision_timestamp for d in decisions))
        return summary

    def shadow_evaluation(self, decisions: list[RouterDecision], *, tca: TcaReport, graph: GraphResult,
                          market_data: MarketData, decision_session: dict[str, int],
                          outcome_model: OutcomeModel) -> ShadowReport:
        features = PointInTimeFeatures(tca, graph, market_data, decision_session)
        model = self.router_models
        s = self.settings.section("shadow")

        def model_for(d: RouterDecision) -> RouterModel:
            return model[d.router_model_version]

        baseline_model = model["1.4"]
        policies = [
            Policy("baseline", baseline_model, lambda d: dict(d.decision_features)),
            Policy("integrity_corrected", baseline_model, features.corrected),
            Policy("graph_enhanced", baseline_model, features.graph_enhanced),
        ]
        sizes = lambda d: "small(<=500)" if d.quantity <= 500 else ("medium(<=1000)" if d.quantity <= 1000 else "large(>1000)")  # noqa: E731
        return evaluate_shadow(
            decisions, policies, outcome_model,
            regime_of=lambda d: graph.regimes.get(decision_session[d.decision_id], MarketRegime.NORMAL).value,
            session_of=lambda d: decision_session[d.decision_id],
            size_bucket_of=sizes,
            min_sessions=int(s["min_sessions"]),
            bootstrap_samples=int(s["bootstrap_samples"]),
            confidence_level=float(s["confidence_level"]),
        )


class PointInTimeFeatures:
    """Router features recomputed from the audit's corrected TCA, using only
    sessions strictly before the decision's session (no look-ahead).

    * corrected:      unconditional adverse selection and impact per venue
    * graph_enhanced: regime-conditional adverse selection, shrunk toward the
                      unconditional estimate; leaders in the lead-lag graph
                      (higher influence) shrink less, because their
                      information-driven adverse selection is more regime
                      dependent. Regime at decision time is estimated from the
                      trailing 60 s realised volatility against thresholds
                      fitted on earlier sessions only.
    """

    SHRINK_N0 = 30.0

    def __init__(self, tca: TcaReport, graph: GraphResult, md: MarketData, decision_session: dict[str, int]):
        self.decision_session = decision_session
        self.influence = graph.influence.scores
        self.ref = tca.reference_horizon_ms
        session_of_ns = _session_lookup(md)
        self.stats: dict[int, dict[str, list[tuple[float, float, float]]]] = defaultdict(lambda: defaultdict(list))
        for f in tca.fills:
            if not f.included or f.venue_key == "UNRESOLVED":
                continue
            h = f.horizons.get(self.ref)
            if h is None or h.adverse_selection_bps is None:
                continue
            s = session_of_ns(f)
            if s is not None:
                self.stats[s][f.venue_key].append((h.adverse_selection_bps, f.impact_beyond_touch_bps or 0.0, f.quantity))
        self.session_regime = graph.regimes
        self._trailing = _TrailingVol(md)
        self._cache: dict[tuple[int, str, str | None], tuple[float | None, int]] = {}

    def _estimate(self, session: int, venue: str, regime: MarketRegime | None) -> tuple[float | None, int]:
        key = (session, venue, regime.value if regime else None)
        if key in self._cache:
            return self._cache[key]
        rows = []
        for s in range(session):
            if regime is not None and self.session_regime.get(s) is not regime:
                continue
            rows.extend(self.stats[s].get(venue, []))
        qty = sum(r[2] for r in rows)
        value = (sum(r[0] * r[2] for r in rows) / qty) if qty > 0 else None
        self._cache[key] = (value, len(rows))
        return value, len(rows)

    def corrected(self, d: RouterDecision) -> dict[str, CandidateFeatures]:
        session = self.decision_session[d.decision_id]
        out = {}
        for venue, f in d.decision_features.items():
            as_hat, n = self._estimate(session, venue, None)
            out[venue] = f if as_hat is None else f.model_copy(update={"adverse_selection_bps": as_hat})
        return out

    def graph_enhanced(self, d: RouterDecision) -> dict[str, CandidateFeatures]:
        session = self.decision_session[d.decision_id]
        regime = self._trailing.regime_at(d, session)
        out = {}
        for venue, f in self.corrected(d).items():
            if regime is None:
                out[venue] = f
                continue
            cond, n = self._estimate(session, venue, regime)
            if cond is None:
                out[venue] = f
                continue
            n0 = self.SHRINK_N0 * (1.0 - 0.5 * self.influence.get(venue, 0.0))
            w = n / (n + n0)
            base = f.adverse_selection_bps
            out[venue] = f.model_copy(update={"adverse_selection_bps": w * cond + (1 - w) * base,
                                              "graph_influence": self.influence.get(venue)})
        return out


class _TrailingVol:
    """Point-in-time regime estimate from trailing 60 s realised volatility."""

    WINDOW_S = 60

    def __init__(self, md: MarketData):
        series = next(iter(md.consolidated.values()))
        self.series = series
        self.md = md
        mids = (series.bid + series.ask) / 2
        self.logp = np.log(mids)
        self.ts = series.ts_ns
        tick = md.grid_tick_ns
        self.window_ticks = self.WINDOW_S * 1_000_000_000 // tick
        # per-session distribution of trailing-window vols (computed on completed sessions only)
        self.session_samples: dict[int, np.ndarray] = {}
        for i, (start, end) in enumerate(series.windows):
            m = (self.ts >= start) & (self.ts <= end)
            r = np.diff(self.logp[m])
            if len(r) <= self.window_ticks:
                continue
            c = np.concatenate([[0.0], np.cumsum(r * r)])
            vols = np.sqrt(c[self.window_ticks:] - c[:-self.window_ticks])
            self.session_samples[i] = vols[:: max(1, self.window_ticks // 4)]
        self._cache: dict[str, MarketRegime | None] = {}

    def regime_at(self, d: RouterDecision, session: int) -> MarketRegime | None:
        if d.decision_id in self._cache:
            return self._cache[d.decision_id]
        prior = [self.session_samples[s] for s in range(session) if s in self.session_samples]
        result = None
        if len(prior) >= 3:
            pooled = np.concatenate(prior)
            hi, lo = np.percentile(pooled, 75), np.percentile(pooled, 25)
            t = to_epoch_ns(d.decision_timestamp)
            m = (self.ts > t - self.WINDOW_S * 1_000_000_000) & (self.ts <= t)
            r = np.diff(self.logp[m])
            if len(r) >= self.window_ticks // 2:
                vol = float(np.sqrt(np.sum(r * r) * self.window_ticks / len(r)))
                result = (MarketRegime.HIGH_VOLATILITY if vol > hi else
                          MarketRegime.LOW_VOLATILITY if vol < lo else MarketRegime.NORMAL)
        self._cache[d.decision_id] = result
        return result


def _session_lookup(md: MarketData):
    series = next(iter(md.consolidated.values()))
    windows = series.windows

    def lookup(fill) -> int | None:
        t = fill.trade_timestamp_ns
        for i, (start, end) in enumerate(windows):
            if start <= t <= end:
                return i
        return None

    return lookup


def _consolidated_session_vol(series, md: MarketData) -> dict[int, float]:
    out = {}
    mids = (series.bid + series.ask) / 2
    for i, (start, end) in enumerate(series.windows):
        m = (series.ts_ns >= start) & (series.ts_ns <= end)
        r = np.diff(np.log(mids[m]))
        out[i] = float(np.sqrt(np.sum(r * r))) if len(r) > 1 else float("nan")
    return out


def data_quality_by_venue(report: ReconciliationReport, tca: TcaReport) -> dict[str, float]:
    """Share of a venue's attributed fills that are free of identity / lineage / data-quality issues."""
    total: dict[str, int] = defaultdict(int)
    bad: dict[str, int] = defaultdict(int)
    tca_by_exec = {(f.source_system, f.exec_id): f for f in tca.fills}
    for e in report.executions:
        if e.venue_key == "UNRESOLVED" or e.quantity <= 0:
            continue
        total[e.venue_key] += 1
        f = tca_by_exec.get((e.source_system, e.original_exec_id))
        issue = (e.lineage_status != "COMPLETE" or e.intended_venue_id is None or (f is not None and not f.included))
        if issue:
            bad[e.venue_key] += 1
    return {v: 1.0 - bad[v] / total[v] for v in total}


def _count(values) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for v in values:
        out[v] += 1
    return out


__all__ = ["AuditPipeline", "GraphResult", "IngestionReport", "NormalizationResult", "PointInTimeFeatures",
           "data_quality_by_venue", "session_returns", "session_volatility"]
