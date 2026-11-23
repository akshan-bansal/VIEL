"""Controlled vocabularies shared by every service.

Design rule: none of these enumerations has a catch-all member that a failed
lookup can silently land in. ``UNKNOWN`` / ``UNRESOLVED`` / ``UNMAPPED`` are
explicit exception states that always travel with the raw value that caused
them. There is intentionally no ``LEGACY`` member in :class:`Classification`.
"""

from __future__ import annotations

from enum import StrEnum


class MappingStatus(StrEnum):
    MAPPED = "MAPPED"
    UNMAPPED = "UNMAPPED"
    AMBIGUOUS = "AMBIGUOUS"
    INVALID = "INVALID"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class Classification(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    OTHER = "OTHER"
    UNRESOLVED = "UNRESOLVED"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    SELL_SHORT = "SELL_SHORT"
    SELL_SHORT_EXEMPT = "SELL_SHORT_EXEMPT"

    @property
    def direction(self) -> int:
        """+1 for buys, -1 for every sell variant (spec §8.1)."""
        return 1 if self is Side.BUY else -1


class ExecType(StrEnum):
    """Canonical execution-report event types.

    FIX 4.2 PARTIAL_FILL / FILL and FIX 4.4 TRADE are all normalised to TRADE;
    FIX 4.2 ExecTransType CORRECT / CANCEL become TRADE_CORRECT / TRADE_CANCEL.
    """

    NEW = "NEW"
    TRADE = "TRADE"
    TRADE_CORRECT = "TRADE_CORRECT"
    TRADE_CANCEL = "TRADE_CANCEL"
    CANCELED = "CANCELED"
    REPLACED = "REPLACED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    DONE_FOR_DAY = "DONE_FOR_DAY"
    PENDING_NEW = "PENDING_NEW"
    PENDING_CANCEL = "PENDING_CANCEL"
    PENDING_REPLACE = "PENDING_REPLACE"
    RESTATED = "RESTATED"
    ORDER_STATUS = "ORDER_STATUS"

    @property
    def is_economic(self) -> bool:
        return self in (ExecType.TRADE, ExecType.TRADE_CORRECT, ExecType.TRADE_CANCEL)


class VenueType(StrEnum):
    EXCHANGE = "EXCHANGE"
    ATS = "ATS"
    MTF = "MTF"
    SINGLE_DEALER_PLATFORM = "SINGLE_DEALER_PLATFORM"
    BROKER_INTERNALIZER = "BROKER_INTERNALIZER"
    OTHER = "OTHER"


class VenueStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"


class IdentifierType(StrEnum):
    """What kind of identifier a raw venue string claims to be."""

    ALIAS = "ALIAS"  # destination code / alias (e.g. FIX ExDestination without 1133=G)
    MIC = "MIC"  # ISO 10383 market identifier code (e.g. FIX LastMkt)


class Severity(StrEnum):
    """Internal operational severity — not a regulatory determination."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class ExceptionType(StrEnum):
    # spec §5.3
    UNKNOWN_DESTINATION = "UNKNOWN_DESTINATION"
    AMBIGUOUS_VENUE = "AMBIGUOUS_VENUE"
    MISSING_ORDER_TYPE = "MISSING_ORDER_TYPE"
    DUPLICATE_EXEC_ID = "DUPLICATE_EXEC_ID"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    UNKNOWN_FIX_TAG = "UNKNOWN_FIX_TAG"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    # identity
    INVALID_VENUE = "INVALID_VENUE"
    UNRESOLVED_EXECUTION_VENUE = "UNRESOLVED_EXECUTION_VENUE"
    # protocol / data quality
    PROTOCOL_VALIDATION_FAILURE = "PROTOCOL_VALIDATION_FAILURE"
    UNKNOWN_ENUM_VALUE = "UNKNOWN_ENUM_VALUE"
    UNKNOWN_ORDER_TYPE_CODE = "UNKNOWN_ORDER_TYPE_CODE"
    INVALID_SIDE = "INVALID_SIDE"
    CLOCK_DRIFT = "CLOCK_DRIFT"
    SEQUENCE_DUPLICATE = "SEQUENCE_DUPLICATE"
    # reconciliation
    CONFLICTING_EXEC_ID = "CONFLICTING_EXEC_ID"
    ORPHAN_EXECUTION = "ORPHAN_EXECUTION"
    ORPHAN_CORRECTION = "ORPHAN_CORRECTION"
    AMBIGUOUS_CORRECTION = "AMBIGUOUS_CORRECTION"
    OVERFILL = "OVERFILL"
    CUMQTY_MISMATCH = "CUMQTY_MISMATCH"
    MISSING_LINEAGE = "MISSING_LINEAGE"
    FILL_AFTER_TERMINAL_STATE = "FILL_AFTER_TERMINAL_STATE"
    UNSUPPORTED_ORDER_EVENT = "UNSUPPORTED_ORDER_EVENT"
    DESTINATION_MISMATCH = "DESTINATION_MISMATCH"
    # analytics / classification
    MARKET_ORDER_LEGACY_CLASSIFICATION = "MARKET_ORDER_LEGACY_CLASSIFICATION"
    STALE_MIDPOINT = "STALE_MIDPOINT"
    GRAPH_TCA_DIVERGENCE = "GRAPH_TCA_DIVERGENCE"
    # router audit
    MISSING_MODEL_FEATURE = "MISSING_MODEL_FEATURE"
    DECISION_NOT_REPRODUCIBLE = "DECISION_NOT_REPRODUCIBLE"
    LOOKAHEAD_FEATURE = "LOOKAHEAD_FEATURE"
    ROUTER_FEATURE_DIVERGENCE = "ROUTER_FEATURE_DIVERGENCE"
    # governance
    MAPPING_VERSION_CHANGE = "MAPPING_VERSION_CHANGE"


class ExceptionStatus(StrEnum):
    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"
    RESOLVED = "RESOLVED"
    REPROCESSED = "REPROCESSED"
    CLOSED = "CLOSED"
    WAIVED = "WAIVED"


class DefectCategory(StrEnum):
    """Defect taxonomy from spec §1 (where a classification change originates)."""

    PROTOCOL_INGESTION = "PROTOCOL_INGESTION"
    VENUE_REFERENCE_DATA = "VENUE_REFERENCE_DATA"
    ORDER_TYPE_NORMALIZATION = "ORDER_TYPE_NORMALIZATION"
    OMS_EMS_ROUTING = "OMS_EMS_ROUTING"
    EXECUTION_ATTRIBUTION = "EXECUTION_ATTRIBUTION"
    ANALYTICS_MODEL_TRAINING = "ANALYTICS_MODEL_TRAINING"
    COMBINATION = "COMBINATION"
    NO_DEFECT = "NO_DEFECT"


class MarketRegime(StrEnum):
    LOW_VOLATILITY = "LOW_VOLATILITY"
    NORMAL = "NORMAL"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNKNOWN = "UNKNOWN"
