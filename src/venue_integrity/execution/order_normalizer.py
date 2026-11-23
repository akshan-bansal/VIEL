"""Order and execution-report normalisation (spec §3.3, §3.4, §5.2).

Critical invariant: Classification ⊥ Venue Identity.

``normalize_order_type`` reads only the FIX OrdType code and ``classify_order``
reads only the resulting :class:`OrderType`. Neither function accepts a venue,
so there is no code path through which a venue lookup can change whether an
order is a market or a limit order. Venue resolution runs separately and its
outcome is stored in its own fields.

Actual execution venue comes only from the execution report's LastMkt(30). The
intended destination (ExDestination(100)) is never used to fill it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from venue_integrity.identity.exception_queue import Finding
from venue_integrity.identity.mic_registry import MicReference, VenueRegistry
from venue_integrity.identity.venue_resolver import resolution_finding, resolve_venue
from venue_integrity.ingestion.fix_parser import FixDictionary, FixMessage
from venue_integrity.schemas.enums import (
    Classification,
    ExceptionType,
    ExecType,
    IdentifierType,
    MappingStatus,
    OrderType,
    Side,
)
from venue_integrity.schemas.execution_event import ExecutionEvent
from venue_integrity.schemas.normalized_order import CLASSIFICATION_BY_ORDER_TYPE, NormalizedOrder
from venue_integrity.schemas.raw_event import RawEvent
from venue_integrity.schemas.venue_identity import VenueResolution
from venue_integrity.timeutil import TimestampError, parse_fix_timestamp, to_epoch_ns

NORMALIZER_VERSION = "1.0.0"

# FIX OrdType(40). Codes with a canonical type map to it; every other valid FIX
# code maps to OTHER (raw code retained). Anything else is UNKNOWN.
_CANONICAL_ORD_TYPE: dict[str, OrderType] = {"1": OrderType.MARKET, "2": OrderType.LIMIT, "3": OrderType.STOP}
_OTHER_ORD_TYPE_CODES = frozenset("456789DEGIJKLMPQ")

_SIDE: dict[str, Side] = {"1": Side.BUY, "2": Side.SELL, "5": Side.SELL_SHORT, "6": Side.SELL_SHORT_EXEMPT}

_EXEC_TYPE: dict[str, ExecType] = {
    "0": ExecType.NEW,
    "1": ExecType.TRADE,  # FIX 4.2 partial fill
    "2": ExecType.TRADE,  # FIX 4.2 fill
    "3": ExecType.DONE_FOR_DAY,
    "4": ExecType.CANCELED,
    "5": ExecType.REPLACED,
    "6": ExecType.PENDING_CANCEL,
    "8": ExecType.REJECTED,
    "A": ExecType.PENDING_NEW,
    "C": ExecType.EXPIRED,
    "D": ExecType.RESTATED,
    "E": ExecType.PENDING_REPLACE,
    "F": ExecType.TRADE,
    "G": ExecType.TRADE_CORRECT,
    "H": ExecType.TRADE_CANCEL,
    "I": ExecType.ORDER_STATUS,
}
# FIX 4.2 ExecTransType(20) applied to fills
_EXEC_TRANS_TYPE: dict[str, ExecType | None] = {"0": None, "1": ExecType.TRADE_CANCEL, "2": ExecType.TRADE_CORRECT, "3": None}


def normalize_order_type(raw_ord_type: str | None) -> OrderType:
    """Map a FIX OrdType(40) code to :class:`OrderType`. Missing/unknown -> UNKNOWN."""
    if raw_ord_type is None or raw_ord_type == "":
        return OrderType.UNKNOWN
    if raw_ord_type in _CANONICAL_ORD_TYPE:
        return _CANONICAL_ORD_TYPE[raw_ord_type]
    if raw_ord_type in _OTHER_ORD_TYPE_CODES:
        return OrderType.OTHER
    return OrderType.UNKNOWN


def classify_order(order_type: OrderType | str) -> Classification:
    """Classification as a pure function of order type (no venue input exists)."""
    return CLASSIFICATION_BY_ORDER_TYPE[OrderType(order_type)]


def normalize_side(raw_side: str | None) -> Side | None:
    return _SIDE.get(raw_side or "")


def _decimal(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(value)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _event_time(message: FixMessage, raw: RawEvent) -> tuple[datetime, int, str]:
    for tag, label in ((60, "TransactTime"), (52, "SendingTime")):
        value = message.get(tag)
        if value:
            try:
                dt, ns = parse_fix_timestamp(value)
                return dt, ns, label
            except TimestampError:
                continue
    return raw.received_at, to_epoch_ns(raw.received_at), "received_at"


@dataclass(frozen=True)
class GovernedRouteRule:
    """An explicitly governed business route category (spec §13.2).

    Matching is on positively identified attributes only; a rule can never match
    because something failed to resolve.
    """

    name: str
    source_system: str | None = None
    session_id: str | None = None
    intended_venue_id: str | None = None

    ALLOWED_KEYS = ("source_system", "session_id", "intended_venue_id")

    @classmethod
    def from_config(cls, item: dict[str, Any]) -> GovernedRouteRule:
        match = item.get("match") or {}
        unknown = set(match) - set(cls.ALLOWED_KEYS)
        if unknown:
            raise ValueError(f"governed route rule {item.get('name')} uses unsupported keys {sorted(unknown)}")
        if not match:
            raise ValueError("governed route rule must match on at least one attribute")
        return cls(name=item["name"], **match)

    def applies(self, source_system: str, session_id: str | None, intended_venue_id: str | None) -> bool:
        if self.source_system is not None and self.source_system != source_system:
            return False
        if self.session_id is not None and self.session_id != session_id:
            return False
        if self.intended_venue_id is not None:
            if intended_venue_id is None or intended_venue_id != self.intended_venue_id:
                return False
        return True


@dataclass
class NormalizationContext:
    registry: VenueRegistry
    dictionary: FixDictionary
    registry_version: str | None = None
    mic_reference: MicReference | None = None
    lastmkt_alias_resolution: bool = True
    governed_rules: tuple[GovernedRouteRule, ...] = ()

    @property
    def version(self) -> str:
        return self.registry_version or self.registry.latest_version


def resolve_intended_venue(message: FixMessage, raw: RawEvent, event_time: datetime, ctx: NormalizationContext) -> VenueResolution:
    id_source = message.get(1133)
    identifier_type = IdentifierType.MIC if id_source == "G" else IdentifierType.ALIAS
    return resolve_venue(
        message.get(100),
        ctx.registry,
        ctx.version,
        event_time=event_time,
        source_system=raw.source_system,
        identifier_type=identifier_type,
        mic_reference=ctx.mic_reference,
    )


def resolve_actual_venue(raw_last_mkt: str | None, raw: RawEvent, event_time: datetime,
                         ctx: NormalizationContext) -> tuple[VenueResolution, str]:
    """LastMkt as a MIC; optionally a registered broker alias. Returns (resolution, attempt trail)."""
    as_mic = resolve_venue(raw_last_mkt, ctx.registry, ctx.version, event_time=event_time,
                           source_system=raw.source_system, identifier_type=IdentifierType.MIC,
                           mic_reference=ctx.mic_reference)
    trail = f"MIC:{as_mic.reason}"
    if as_mic.mapping_status is MappingStatus.UNMAPPED and raw_last_mkt and ctx.lastmkt_alias_resolution:
        as_alias = resolve_venue(raw_last_mkt, ctx.registry, ctx.version, event_time=event_time,
                                 source_system=raw.source_system, identifier_type=IdentifierType.ALIAS,
                                 mic_reference=ctx.mic_reference)
        trail += f";ALIAS:{as_alias.reason}"
        if as_alias.mapping_status is not MappingStatus.UNMAPPED:
            return as_alias, trail
    return as_mic, trail


def normalize_new_order(
    message: FixMessage, raw: RawEvent, ctx: NormalizationContext, processing_time: datetime
) -> tuple[NormalizedOrder | None, list[Finding]]:
    findings: list[Finding] = []
    eid = raw.event_id
    cl_ord_id = message.get(11)
    side = normalize_side(message.get(54))
    quantity = _decimal(message.get(38))
    symbol = message.get(55)
    problems = []
    if not cl_ord_id:
        problems.append("ClOrdID(11) missing")
    if side is None:
        problems.append(f"Side(54)={message.get(54)!r} not recognised")
    if quantity is None or quantity <= 0:
        problems.append(f"OrderQty(38)={message.get(38)!r} invalid")
    if not symbol:
        problems.append("Symbol(55) missing")
    if problems:
        findings.append(Finding(ExceptionType.MISSING_LINEAGE, "order not normalisable: " + "; ".join(problems),
                                source_event_id=eid, entity_key=cl_ord_id))
        return None, findings

    raw_ord_type = message.get(40)
    order_type = normalize_order_type(raw_ord_type)
    if order_type is OrderType.UNKNOWN and raw_ord_type:
        findings.append(Finding(ExceptionType.UNKNOWN_ORDER_TYPE_CODE,
                                f"OrdType(40)={raw_ord_type!r} not a recognised code; order kept as UNKNOWN",
                                source_event_id=eid, entity_key="40", raw_value=raw_ord_type))
    elif order_type is OrderType.UNKNOWN:
        findings.append(Finding(ExceptionType.MISSING_ORDER_TYPE, "OrdType(40) missing; order kept as UNKNOWN",
                                source_event_id=eid, entity_key="40"))
    classification = classify_order(order_type)

    event_time, _, _ = _event_time(message, raw)
    resolution = resolve_intended_venue(message, raw, event_time, ctx)
    if resolution.raw_value:  # absent ExDestination = broker discretion, not an identity failure
        finding = resolution_finding(resolution, source_event_id=eid, field="ExDestination(100)")
        if finding is not None:
            findings.append(finding)

    route_category = None
    for rule in ctx.governed_rules:
        if rule.applies(raw.source_system, raw.session_id, resolution.venue_id):
            route_category = rule.name
            break

    dictionary = ctx.dictionary
    parent_tag = dictionary.parent_order_id_tag
    client_tag = dictionary.client_order_id_tag
    order = NormalizedOrder(
        event_id=eid,
        source_system=raw.source_system,
        session_id=raw.session_id,
        event_time=event_time,
        cl_ord_id=cl_ord_id,
        orig_cl_ord_id=message.get(41),
        client_order_id=message.get(client_tag) if client_tag else None,
        parent_order_id=message.get(parent_tag) if parent_tag else None,
        child_order_id=cl_ord_id,
        instrument_id=symbol,
        side=side,
        order_quantity=quantity,
        limit_price=_decimal(message.get(44)),
        raw_ord_type=raw_ord_type,
        order_type=order_type,
        time_in_force=dictionary.enum_name(59, message.get(59)) if message.get(59) else None,
        intended_venue_id=resolution.venue_id,
        raw_ex_destination=message.get(100),
        raw_ex_destination_id_source=message.get(1133),
        classification=classification,
        mapping_status=resolution.mapping_status,
        mapping_reason=resolution.reason,
        mapping_version=resolution.mapping_version,
        route_category=route_category,
        normalized_at=processing_time,
        normalizer_version=NORMALIZER_VERSION,
    )
    if order.parent_order_id is None:
        findings.append(Finding(ExceptionType.MISSING_LINEAGE, "order has no parent order linkage",
                                source_event_id=eid, entity_key=cl_ord_id))
    return order, findings


def canonical_exec_type(message: FixMessage, protocol: str, dictionary: FixDictionary) -> ExecType | None:
    raw = message.get(150)
    exec_type = _EXEC_TYPE.get(raw or "")
    if exec_type is ExecType.TRADE and dictionary.correction_semantics(protocol) == "EXEC_TRANS_TYPE":
        trans = _EXEC_TRANS_TYPE.get(message.get(20) or "0")
        if trans is not None:
            return trans
    return exec_type


def normalize_execution_report(
    message: FixMessage, raw: RawEvent, ctx: NormalizationContext
) -> tuple[ExecutionEvent | None, list[Finding]]:
    findings: list[Finding] = []
    eid = raw.event_id
    exec_id = message.get(17)
    exec_type = canonical_exec_type(message, message.begin_string or "", ctx.dictionary)
    side = normalize_side(message.get(54))
    symbol = message.get(55)
    problems = []
    if not exec_id:
        problems.append("ExecID(17) missing")
    if exec_type is None:
        problems.append(f"ExecType(150)={message.get(150)!r} not recognised")
    if side is None:
        problems.append(f"Side(54)={message.get(54)!r} not recognised")
    if not symbol:
        problems.append("Symbol(55) missing")
    if problems:
        findings.append(Finding(ExceptionType.PROTOCOL_VALIDATION_FAILURE,
                                "execution report not normalisable: " + "; ".join(problems),
                                source_event_id=eid, entity_key=exec_id))
        return None, findings

    event_time, event_ns, _ = _event_time(message, raw)
    raw_last_mkt = message.get(30)
    last_qty = _decimal(message.get(32))
    last_px = _decimal(message.get(31))

    if exec_type.is_economic:
        resolution, trail = resolve_actual_venue(raw_last_mkt, raw, event_time, ctx)
        finding = resolution_finding(resolution, source_event_id=eid, field="LastMkt(30)", execution=True)
        if finding is not None:
            findings.append(finding)
        mapping_status, venue_id, mic = resolution.mapping_status, resolution.venue_id, resolution.mic
        reason, version = trail, resolution.mapping_version
    else:
        mapping_status, venue_id, mic = MappingStatus.UNMAPPED, None, None
        reason, version = "NOT_APPLICABLE_NON_ECONOMIC", ctx.version

    if exec_type in (ExecType.TRADE, ExecType.TRADE_CORRECT):
        if last_qty is None or last_qty <= 0 or last_px is None or last_px <= 0:
            findings.append(Finding(ExceptionType.PROTOCOL_VALIDATION_FAILURE,
                                    f"{exec_type} without positive LastQty/LastPx",
                                    source_event_id=eid, entity_key=exec_id))
            return None, findings
    if exec_type.is_economic and exec_type is not ExecType.TRADE and not message.get(19):
        findings.append(Finding(ExceptionType.ORPHAN_CORRECTION, f"{exec_type} without ExecRefID(19)",
                                source_event_id=eid, entity_key=exec_id))
        return None, findings

    parent_tag = ctx.dictionary.parent_order_id_tag
    event = ExecutionEvent(
        exec_id=exec_id,
        exec_ref_id=message.get(19),
        exec_type=exec_type,
        raw_exec_type=message.get(150),
        raw_exec_trans_type=message.get(20),
        source_event_id=eid,
        source_system=raw.source_system,
        session_id=raw.session_id,
        sequence_number=raw.sequence_number,
        poss_dup=message.get(43) == "Y",
        order_id=message.get(37),
        child_order_id=message.get(11),
        parent_order_id=message.get(parent_tag) if parent_tag else None,
        instrument_id=symbol,
        side=side,
        ord_status=message.get(39),
        raw_last_mkt=raw_last_mkt,
        actual_venue_id=venue_id,
        venue_mic=mic,
        mapping_status=mapping_status,
        mapping_reason=reason,
        mapping_version=version,
        executed_quantity=last_qty if (last_qty is not None and exec_type.is_economic) else Decimal(0),
        execution_price=last_px if exec_type.is_economic else None,
        cum_qty=_decimal(message.get(14)),
        leaves_qty=_decimal(message.get(151)),
        execution_timestamp=event_time,
        execution_timestamp_ns=event_ns,
        payload_hash=raw.payload_hash,
    )
    return event, findings
