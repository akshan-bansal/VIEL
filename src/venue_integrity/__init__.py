"""Venue Integrity Audit (VIA).

Replay/simulation-mode audit of market-venue identity, order classification,
execution attribution, graph-derived venue influence and Smart Order Router
decisions. The package contains no order-entry or session-layer code.
"""

from venue_integrity.execution.order_normalizer import classify_order, normalize_order_type
from venue_integrity.identity.mic_registry import MicReference, VenueRegistry
from venue_integrity.identity.venue_resolver import resolve_venue
from venue_integrity.schemas.enums import (
    Classification,
    ExceptionType,
    IdentifierType,
    MappingStatus,
    OrderType,
    Severity,
    Side,
)

__version__ = "1.0.0"

__all__ = [
    "Classification",
    "ExceptionType",
    "IdentifierType",
    "MappingStatus",
    "MicReference",
    "OrderType",
    "Severity",
    "Side",
    "VenueRegistry",
    "__version__",
    "classify_order",
    "normalize_order_type",
    "resolve_venue",
]
