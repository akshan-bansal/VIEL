"""Canonical data contracts A–F."""

from venue_integrity.schemas.enums import (
    Classification,
    DefectCategory,
    ExceptionStatus,
    ExceptionType,
    ExecType,
    IdentifierType,
    MappingStatus,
    MarketRegime,
    OrderType,
    Severity,
    Side,
    VenueStatus,
    VenueType,
)
from venue_integrity.schemas.execution_event import ExecutionEvent
from venue_integrity.schemas.normalized_order import CLASSIFICATION_BY_ORDER_TYPE, NormalizedOrder
from venue_integrity.schemas.raw_event import RawCapture, RawEvent
from venue_integrity.schemas.router_decision import (
    CandidateFeatures,
    ExcludedVenue,
    RouterDecision,
    RouterDecisionOutcome,
)
from venue_integrity.schemas.venue_identity import ResolutionCandidate, VenueRecord, VenueResolution
from venue_integrity.schemas.venue_intelligence import SCORE_DEFINITIONS, VenueIntelligence

__all__ = [
    "CLASSIFICATION_BY_ORDER_TYPE",
    "SCORE_DEFINITIONS",
    "CandidateFeatures",
    "Classification",
    "DefectCategory",
    "ExcludedVenue",
    "ExceptionStatus",
    "ExceptionType",
    "ExecType",
    "ExecutionEvent",
    "IdentifierType",
    "MappingStatus",
    "MarketRegime",
    "NormalizedOrder",
    "OrderType",
    "RawCapture",
    "RawEvent",
    "ResolutionCandidate",
    "RouterDecision",
    "RouterDecisionOutcome",
    "Severity",
    "Side",
    "VenueIntelligence",
    "VenueRecord",
    "VenueResolution",
    "VenueStatus",
    "VenueType",
]
