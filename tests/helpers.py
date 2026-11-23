"""Builders for hand-crafted FIX traffic used across the test suite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from venue_integrity.ingestion.fix_parser import build_fix
from venue_integrity.pipeline import AuditPipeline
from venue_integrity.schemas.raw_event import RawCapture

T0 = datetime(2026, 11, 5, 14, 30, tzinfo=UTC)


def ts(offset_ms: float = 0) -> str:
    t = T0 + timedelta(milliseconds=offset_ms)
    return t.strftime("%Y%m%d-%H:%M:%S.") + f"{t.microsecond:06d}"


def header(msg_type: str, seq: int, offset_ms: float, sender="FIRM_OMS", target="BROKER_A", poss_dup=False):
    fields = [(35, msg_type), (49, sender), (56, target), (34, str(seq))]
    if poss_dup:
        fields.append((43, "Y"))
    fields.append((52, ts(offset_ms)))
    return fields


def order_msg(cl_ord_id: str, *, ord_type: str | None = "1", dest: str | None = "ABC", side: str = "1", qty: int = 500,
              parent: str | None = "P1", seq: int = 1, offset_ms: float = 0, begin: str = "FIX.4.4",
              id_source: str | None = None, price: str | None = None, extra=()) -> str:
    body = header("D", seq, offset_ms) + [(11, cl_ord_id)]
    if parent:
        body.append((583, parent))
    body += [(9001, "CL-01"), (55, "INSTRUMENT_001"), (54, side), (38, str(qty))]
    if ord_type is not None:
        body.append((40, ord_type))
    if price is not None:
        body.append((44, price))
    body += [(59, "0"), (60, ts(offset_ms))]
    if dest is not None:
        body.append((100, dest))
    if id_source is not None:
        body.append((1133, id_source))
    body += list(extra)
    return build_fix(body, begin_string=begin)


def exec_msg(exec_id: str, cl_ord_id: str, *, exec_type: str = "F", qty: int | None = 100, px: str | None = "101.25",
             last_mkt: str | None = "XABC", cum: int | None = None, leaves: int | None = None, ref: str | None = None,
             seq: int = 1, offset_ms: float = 1, begin: str = "FIX.4.4", ord_status: str = "1",
             exec_trans_type: str | None = None, order_qty: int = 500, parent: str | None = "P1", poss_dup=False,
             side: str = "1", sending_offset_ms: float | None = None, extra=()) -> str:
    """``offset_ms`` sets TransactTime(60); ``sending_offset_ms`` (default: same) sets SendingTime(52)."""
    sending = offset_ms if sending_offset_ms is None else sending_offset_ms
    body = header("8", seq, sending, sender="BROKER_A", target="FIRM_OMS", poss_dup=poss_dup)
    body += [(37, "ORD-1"), (11, cl_ord_id)]
    if parent:
        body.append((583, parent))
    body += [(55, "INSTRUMENT_001"), (54, side), (38, str(order_qty)), (17, exec_id)]
    if ref:
        body.append((19, ref))
    if exec_trans_type is not None:
        body.append((20, exec_trans_type))
    body += [(150, exec_type), (39, ord_status)]
    if qty is not None:
        body.append((32, str(qty)))
    if px is not None:
        body.append((31, px))
    if last_mkt is not None:
        body.append((30, last_mkt))
    if cum is not None:
        body.append((14, str(cum)))
    if leaves is not None:
        body.append((151, str(leaves)))
    body.append((60, ts(offset_ms)))
    body += list(extra)
    return build_fix(body, begin_string=begin)


def capture(payload: str, *, source: str = "BROKER_A", session: str = "S1", offset_ms: float = 2) -> RawCapture:
    return RawCapture(source_system=source, session_id=session, received_at=T0 + timedelta(milliseconds=offset_ms),
                      raw_payload=payload)


def run_flow(order_payloads: list[str], exec_payloads: list[str], *, registry=None, source: str = "BROKER_A"):
    """Ingest orders (session ORD) and executions (session EXEC) and reconcile."""
    pipeline = AuditPipeline(registry=registry, mode="replay")
    caps = [capture(p, source=source, session="ORD", offset_ms=i) for i, p in enumerate(order_payloads)]
    caps += [capture(p, source=source, session="EXEC", offset_ms=1000 + i) for i, p in enumerate(exec_payloads)]
    pipeline.ingest(caps)
    normalized = pipeline.normalize()
    report = pipeline.reconcile(normalized)
    return pipeline, normalized, report
