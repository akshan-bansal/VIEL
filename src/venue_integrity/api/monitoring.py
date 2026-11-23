"""Prometheus metrics for the Venue Integrity dashboard (spec §13.1).

A dedicated registry per application instance keeps tests isolated.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        self.ingested = Counter("via_ingested_events_total", "Captured events by ingestion outcome", ["outcome"], registry=r)
        self.validation = Counter("via_validation_total", "Validation status of stored events", ["status"], registry=r)
        self.unresolved = Gauge("via_unresolved_exceptions", "Open exceptions", ["exception_type", "severity"], registry=r)
        self.alerts = Gauge("via_alerts", "Alerts raised since start", ["code"], registry=r)
        self.mapping_coverage = Gauge("via_mapping_coverage_ratio", "Share of venue identifiers that resolved (orders + fills)",
                                      registry=r)
        self.unmapped_volume = Gauge("via_unmapped_destination_volume", "Order quantity with unresolved destination", registry=r)
        self.silent_coercions = Gauge("via_silent_coercions", "Orders whose classification differs from their raw order type "
                                      "(required control: 0)", registry=r)
        self.silent_fallbacks = Gauge("via_silent_venue_fallbacks", "Executions with a venue but no LastMkt (required: 0)",
                                      registry=r)
        self.lineage_ratio = Gauge("via_execution_lineage_ratio", "Share of fills with complete lineage", registry=r)
        self.attribution_ratio = Gauge("via_actual_venue_attribution_ratio", "Share of executed quantity with resolved venue",
                                       registry=r)
        self.sequence_gaps = Gauge("via_sequence_gaps_open", "Missing sequence numbers not yet recovered", registry=r)
        self.registry_version = Gauge("via_registry_version_info", "Current registry version", ["version"], registry=r)
        self.market_legacy = Gauge("via_market_orders_classified", "Market orders by classification", ["classification"],
                                   registry=r)

    def render(self) -> bytes:
        return generate_latest(self.registry)
