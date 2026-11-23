"""Reconstruction of the suspected legacy classification defect.

FOR DIFFERENTIAL TESTING AND DEMONSTRATION ONLY. Nothing in the audit pipeline
uses this module to classify orders.

The hypothesised defect (spec §1): the legacy normaliser derived an order's
classification from a per-destination lookup and fell back to ``"LEGACY"`` when
the destination was not in its alias table. A market order routed through a
newly introduced alias was therefore reported as LEGACY. Because the SOR sends
most aggressive flow to the most influential venue, a new alias for that venue
concentrates the defect exactly where venue influence is highest — producing
the observed pattern "significant venue influence + market order -> LEGACY"
without any real legacy route being involved.

The reconstruction also reproduces a second latent defect: flattening
broker-specific aliases into a single table silently resolves an ambiguous
alias to whichever record was loaded last.
"""

from __future__ import annotations

from dataclasses import dataclass

from venue_integrity.identity.mic_registry import VenueRegistry

LEGACY_LABEL = "LEGACY"
_LEGACY_ORD_TYPE = {"1": "MARKET", "2": "LIMIT"}


def legacy_alias_table(registry: VenueRegistry, version: str | None = None) -> dict[str, str]:
    """Flatten a registry version the way the legacy loader did: last record wins,
    no broker scoping, no effective dating, no MIC validation."""
    table: dict[str, str] = {}
    for record in registry.records_at(version):
        for alias in record.raw_aliases:
            table[alias] = record.venue_id
        for aliases in record.broker_aliases.values():
            for alias in aliases:
                table[alias] = record.venue_id
    return table


@dataclass(frozen=True)
class LegacyStageTrace:
    """What each stage of the legacy path produced for one order."""

    protocol_ord_type: str | None  # stage 1: raw FIX OrdType as parsed
    protocol_label: str  # stage 1 label derived from the raw code
    venue_lookup: str | None  # stage 2: legacy alias-table result (None = not found)
    normalized_label: str  # stage 3: legacy normaliser output
    oms_label: str  # stage 4: label carried in the OMS order record
    analytics_label: str  # stage 5: label in the analytics / model-training dataset


class LegacyNormalizer:
    def __init__(self, alias_table: dict[str, str]):
        self.alias_table = dict(alias_table)

    def classify(self, raw_ord_type: str | None, raw_ex_destination: str | None) -> str:
        venue = self.alias_table.get(raw_ex_destination or "")
        if venue is None:
            return LEGACY_LABEL  # <- the defect: venue failure overwrites classification
        return _LEGACY_ORD_TYPE.get(raw_ord_type or "", "OTHER")

    def trace(self, raw_ord_type: str | None, raw_ex_destination: str | None) -> LegacyStageTrace:
        protocol_label = _LEGACY_ORD_TYPE.get(raw_ord_type or "", "OTHER")
        venue = self.alias_table.get(raw_ex_destination or "")
        label = self.classify(raw_ord_type, raw_ex_destination)
        return LegacyStageTrace(
            protocol_ord_type=raw_ord_type,
            protocol_label=protocol_label,
            venue_lookup=venue,
            normalized_label=label,
            oms_label=label,
            analytics_label=label,
        )
